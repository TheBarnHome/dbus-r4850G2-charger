#!/usr/bin/env python3
"""Setpoint policy for the Huawei R4850G2 on a Victron DC bus.

Three modes, one output: a (voltage, current limit, standby) triple that the
D-Bus driver writes to the rectifier over CAN.  The module is pure: no D-Bus,
no CAN, no clock.  It takes a battery snapshot and a monotonic timestamp in
seconds, and returns the setpoints.  That is what makes the behaviour testable
off-target.

Modes
-----
OFF
    Output inhibited (standby register), current limit zero.

COMPENSATION
    Keep the battery current at zero: the rectifier carries the load instead of
    the battery.  The voltage setpoint follows the *measured* battery voltage
    plus a small delta, capped by the BMS charge voltage limit (CVL).  The
    delta matters: a rectifier only delivers current if its setpoint is above
    the bus, and capping it at ``v_bat + delta`` keeps the bus from ever being
    pulled to a voltage the battery has not asked for, even if the load
    disappears.  The current limit is the output of a PI regulator on the
    battery current, in velocity form, clamped and slew limited.

RECHARGE
    Charge at full rectifier power.  The voltage setpoint is the BMS CVL,
    never above it; the current limit is the smaller of the configured maximum
    and the BMS CCL.  Without a CVL the mode is refused: charging a LiFePO4
    pack on a guessed voltage is exactly the failure this driver exists to
    avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from r4850_can import VOLTAGE_MAX, VOLTAGE_MIN


class Mode(IntEnum):
    OFF = 0
    COMPENSATION = 1
    RECHARGE = 2


MODE_NAMES = {Mode.OFF: "off", Mode.COMPENSATION: "compensation", Mode.RECHARGE: "recharge"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class BatteryState:
    """Snapshot of the BMS.  ``None`` means 'not available right now'."""

    voltage: float | None = None
    current: float | None = None  # + charging, - discharging
    charge_voltage_limit: float | None = None  # CVL, /Info/MaxChargeVoltage
    charge_current_limit: float | None = None  # CCL, /Info/MaxChargeCurrent


@dataclass(frozen=True)
class Setpoints:
    voltage: float
    current_limit: float
    standby: bool
    mode: Mode
    reason: str

    def as_tuple(self) -> tuple[float, float, bool]:
        return (round(self.voltage, 1), round(self.current_limit, 1), self.standby)


@dataclass
class RegulatorConfig:
    v_min: float = VOLTAGE_MIN
    v_max: float = VOLTAGE_MAX
    # Absolute ceiling for the rectifier output current, in amps.  The R4850G2
    # is 3000 W / 50 A nominal; keep the default below the 122 % CAN maximum.
    max_current_a: float = 50.0
    # Voltage offset applied in compensation mode so the rectifier can source
    # current without pushing the bus above what the battery asks for.
    compensation_delta_v: float = 0.5
    # PI on the battery current.  Velocity form: u += kp * de + ki * e * dt.
    kp: float = 0.5  # A per A of error
    ki: float = 0.5  # A per A.second
    # Ignore errors inside this band to stop the setpoint hunting at 1 Hz.
    deadband_a: float = 0.3
    # Slew limit on the current setpoint, amps per second.  Decreasing is
    # deliberately faster than increasing: when the load collapses the
    # rectifier must stop pushing before the surplus reaches the battery.
    ramp_a_per_s: float = 10.0
    ramp_down_multiplier: float = 3.0
    # Battery readings older than this are treated as missing (seconds).
    battery_stale_s: float = 10.0
    # Loop period assumed for the very first tick (no previous timestamp).
    nominal_dt_s: float = 1.0
    # Never integrate more than this in one step, whatever the clock says.
    max_dt_s: float = 5.0


@dataclass
class Regulator:
    config: RegulatorConfig = field(default_factory=RegulatorConfig)
    _current: float = 0.0
    _last_error: float | None = None
    _last_time: float | None = None
    _nominal_voltage: float = 53.0

    # -- helpers ----------------------------------------------------------

    def _safe_voltage(self, battery: BatteryState) -> float:
        """A voltage we are always willing to write, whatever the BMS says."""
        candidate = battery.voltage
        if candidate is None:
            candidate = battery.charge_voltage_limit
        if candidate is None:
            candidate = self._nominal_voltage
        if battery.charge_voltage_limit is not None:
            candidate = min(candidate, battery.charge_voltage_limit)
        return _clamp(candidate, self.config.v_min, self.config.v_max)

    def _setpoints(
        self, voltage: float, current: float, standby: bool, mode: Mode, reason: str
    ) -> Setpoints:
        voltage = _clamp(voltage, self.config.v_min, self.config.v_max)
        current = _clamp(current, 0.0, self.config.max_current_a)
        self._current = current
        return Setpoints(voltage, current, standby, mode, reason)

    def _slew(self, target: float, dt: float) -> float:
        up = self.config.ramp_a_per_s * max(dt, 0.0)
        down = up * self.config.ramp_down_multiplier
        return _clamp(target, self._current - down, self._current + up)

    # -- public API -------------------------------------------------------

    def step(
        self, mode: Mode, battery: BatteryState, now: float, battery_age_s: float = 0.0
    ) -> Setpoints:
        if self._last_time is None:
            dt = self.config.nominal_dt_s
        else:
            dt = min(max(0.0, now - self._last_time), self.config.max_dt_s)
        self._last_time = now

        if battery.voltage is not None:
            self._nominal_voltage = battery.voltage

        if mode is Mode.OFF:
            self._last_error = None
            return self._setpoints(
                self._safe_voltage(battery), 0.0, True, Mode.OFF, "mode is off"
            )

        if battery_age_s > self.config.battery_stale_s:
            self._last_error = None
            return self._setpoints(
                self._safe_voltage(battery),
                0.0,
                True,
                mode,
                f"battery data stale ({battery_age_s:.0f}s)",
            )

        if mode is Mode.COMPENSATION:
            if battery.voltage is None or battery.current is None:
                self._last_error = None
                return self._setpoints(
                    self._safe_voltage(battery),
                    0.0,
                    True,
                    mode,
                    "battery voltage/current unavailable",
                )

            # The offset must survive: it is what lets the rectifier source
            # current at all.  The ceiling is the BMS CVL, never the present
            # battery voltage, otherwise the offset is clipped away.
            cvl = battery.charge_voltage_limit
            ceiling = self.config.v_max if cvl is None else min(cvl, self.config.v_max)
            voltage = min(battery.voltage + self.config.compensation_delta_v, ceiling)

            error = 0.0 - battery.current
            if abs(error) < self.config.deadband_a:
                error = 0.0
            if self._last_error is None:
                self._last_error = error
            delta = self.config.kp * (error - self._last_error) + self.config.ki * error * dt
            self._last_error = error

            target = _clamp(self._current + delta, 0.0, self.config.max_current_a)
            slewed = self._slew(target, dt)
            return self._setpoints(
                voltage,
                slewed,
                False,
                Mode.COMPENSATION,
                f"holding battery at 0 A (measured {battery.current:+.1f} A)",
            )

        # Mode.RECHARGE
        cvl = battery.charge_voltage_limit
        if cvl is None:
            self._last_error = None
            return self._setpoints(
                self._safe_voltage(battery),
                0.0,
                True,
                mode,
                "no BMS charge voltage limit (CVL) — refusing to charge",
            )

        ceiling = _clamp(cvl, self.config.v_min, self.config.v_max)
        limit = self.config.max_current_a
        if battery.charge_current_limit is not None:
            limit = min(limit, battery.charge_current_limit)
        if battery.voltage is not None and battery.voltage >= ceiling:
            # At or above the requested voltage: hold, do not push current.
            limit = 0.0
        self._last_error = None
        slewed = self._slew(limit, dt) if limit > 0 else 0.0
        return self._setpoints(
            ceiling,
            slewed,
            False,
            Mode.RECHARGE,
            f"charging at {slewed:.1f} A up to {ceiling:.1f} V",
        )
