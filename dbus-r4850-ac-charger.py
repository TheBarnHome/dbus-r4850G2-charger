#!/usr/bin/env python3
"""Huawei R4850G2 as a Victron AC charger on the DC bus.

D-Bus service ``com.victronenergy.charger.huawei-r4850g2.<serial>`` fed by the
rectifier's own CAN telemetry, with the BMS as the authority on charge voltage
and current.  Three modes, one control loop at 1 Hz.

    off           output inhibited (standby register), current limit zero
    compensation  rectifier carries the load, battery held at 0 A
    recharge      full rectifier power up to the BMS CVL

Everything that can hurt the battery lives in ``r4850_modes`` (pure, tested).
This file is the I/O shell: SocketCAN, D-Bus, files, the clock.

Usage
-----
    ./dbus-r4850-ac-charger.py --can can1 --device-instance 10
    ./dbus-r4850-ac-charger.py --self-test 10              # headless, no D-Bus
    ./dbus-r4850-ac-charger.py --test                      # virtual module, no measurements
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
import time
from pathlib import Path

VERSION = "v0.2"

DEFAULT_STATE_DIR = Path("/data/etc/dbus-r4850-state")
DEFAULT_CONFIG = DEFAULT_STATE_DIR / "settings.json"

MODE_OFF = 0
MODE_COMPENSATION = 1
MODE_RECHARGE = 2

# Victron charger /State values.
STATE_OFF = 0
STATE_FAULT = 2
STATE_BULK = 3
STATE_ABSORPTION = 4

# /Mode on com.victronenergy.charger is 0 = off, 1 = on (2 = error in the
# official register map; we never publish it).  Accepting 2 as a *write* is a
# private extension so Home Assistant can reach the third mode through Modbus,
# which only exposes /Mode and /Ac/In/CurrentLimit as writable for this type.
VE_MODE_OFF = 0
VE_MODE_ON = 1
VE_MODE_RECHARGE = 2

# Rectifier efficiency and assumed AC voltage, used to turn the AC current limit
# (the only other writable register for this service type) into a DC ceiling.
AC_NOMINAL_V = 230.0
AC_EFFICIENCY = 0.93

CAN_STALE_S = 5.0
BATTERY_STALE_S = 12.0
# The module falls back to its persistent defaults after ~60 s without traffic,
# so the cached setpoints stop being true. Re-send everything well before that.
REWRITE_EVERY_S = 30.0
# Until the module answers, ask for its info again at this interval. It may be
# powered up long after the service started, or swapped.
INFO_RETRY_S = 5.0
MAX_CURRENT_CEILING = 60.0

log = logging.getLogger("r4850")


# ---------------------------------------------------------------------------
# Persistent settings
# ---------------------------------------------------------------------------


class Settings:
    """Mode and limits, outside the Git checkout so firmware updates keep them."""

    def __init__(self, path: Path, defaults: dict) -> None:
        self.path = path
        self.values = dict(defaults)
        self._load()

    def _load(self) -> None:
        try:
            self.values.update(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            log.warning("réglages illisibles (%s), valeurs par défaut", exc)
            try:
                self.path.rename(self.path.with_suffix(".corrupt"))
            except OSError:
                pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("réglages non enregistrés: %s", exc)


# ---------------------------------------------------------------------------
# Battery view
# ---------------------------------------------------------------------------


class BatteryMonitor:
    """Reads CVL/CCL/voltage/current from whichever BMS service is on the bus."""

    PATHS = {
        "charge_voltage_limit": "/Info/MaxChargeVoltage",
        "charge_current_limit": "/Info/MaxChargeCurrent",
        "voltage": "/Dc/0/Voltage",
        "current": "/Dc/0/Current",
    }

    def __init__(self, explicit_service: str | None = None) -> None:
        self._explicit = explicit_service
        self.service: str | None = None
        self._items: dict = {}
        self.last_ok = 0.0

    def refresh(self, dbus, item_import, connection) -> None:
        """(Re)bind to the battery service; cheap when already bound."""
        service = self._explicit or self._find(dbus)
        if service == self.service and self._items:
            return
        self.service = service
        self._items = {}
        if not service:
            return
        try:
            bus = connection()
            for key, path in self.PATHS.items():
                self._items[key] = item_import(bus, service, path, createsignal=False)
            log.info("BMS suivie: %s", service)
        except Exception:  # noqa: BLE001
            log.exception("import D-Bus de %s impossible", service)
            self._items = {}

    def _find(self, dbus) -> str | None:
        try:
            bus = dbus.SystemBus()
            obj = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            names = obj.ListNames(dbus_interface="org.freedesktop.DBus")
        except Exception:  # noqa: BLE001
            return None
        candidates = sorted(n for n in names if n.startswith("com.victronenergy.battery."))
        return candidates[0] if candidates else None

    def read(self) -> dict:
        out = {}
        for key, item in self._items.items():
            try:
                out[key] = item.get_value()
            except Exception:  # noqa: BLE001
                out[key] = None
        if out.get("voltage") is not None:
            self.last_ok = time.monotonic()
        return out

    @property
    def age(self) -> float:
        return float("inf") if not self.last_ok else time.monotonic() - self.last_ok



# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class R4850Service:
    def __init__(self, args) -> None:
        self.args = args
        self.settings = Settings(
            Path(args.settings_file),
            {
                "mode": MODE_OFF,
                "last_active_mode": MODE_COMPENSATION,
                "max_current": 50.0,
                "compensation_delta": 0.5,
                "ac_current_limit": 16.0,
            },
        )
        self.transport = None
        self.regulator = None
        self.battery = None
        self.last_can_tx = 0.0
        self.last_info_tx = 0.0
        self.last_apply = 0.0
        self.sim_battery_current = -float(args.test_load or 0.0)
        self.last_values: dict = {}

    # -- setup ------------------------------------------------------------

    def load_modules(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from r4850_modes import BatteryState, Mode, Regulator, RegulatorConfig

        self.Mode = Mode
        self.BatteryState = BatteryState
        self.regulator = Regulator(
            RegulatorConfig(
                max_current_a=float(self.settings.values["max_current"]),
                compensation_delta_v=float(self.settings.values["compensation_delta"]),
            )
        )

    def open_transport(self):
        here = str(Path(__file__).resolve().parent)
        sys.path.insert(0, here)
        if self.args.test:
            # Virtual module: it acknowledges the setpoints and nothing else.
            from r4850_transport import FakeTransport

            self.transport = FakeTransport(telemetry=False)
        elif self.args.dry_run or self.args.self_test:
            from r4850_transport import FakeTransport

            self.transport = FakeTransport()
        else:
            from r4850_transport import CanTransport

            self.transport = CanTransport(self.args.can, self.args.address)
        self.transport.open()
        if self.args.test:
            log.warning(
                "MODE TEST : module virtuel sur %s, aucune mesure de tension/courant "
                "publiée et aucun setpoint écrit sur le CAN réél",
                self.args.can,
            )
        return self.transport

    # -- D-Bus service ----------------------------------------------------

    def create_service(self) -> None:
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here / "velib_python"))
        import dbus
        from vedbus import VeDbusItemImport, VeDbusService

        self.dbus = dbus
        self.VeDbusItemImport = VeDbusItemImport
        serial = self.transport.state.serial or "unknown"
        name = f"com.victronenergy.charger.huawei-r4850g2.{serial}"
        svc = VeDbusService(name, register=False)
        self.service = svc

        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", f"{VERSION} on Python {platform.python_version()}")
        svc.add_path("/Mgmt/Connection", f"CAN {self.args.can} @125k")
        svc.add_path("/DeviceInstance", self.args.device_instance)
        svc.add_path("/ProductId", 0xB060)
        svc.add_path("/ProductName", "Huawei R4850G2")
        svc.add_path("/FirmwareVersion", self.transport.state.firmware)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Serial", serial)
        svc.add_path("/Connected", 0)
        svc.add_path("/NrOfOutputs", 1)
        for path in (
            "/Dc/0/Voltage",
            "/Dc/0/Current",
            "/Dc/0/Power",
            "/Dc/0/Temperature",
            "/Ac/In/0/L1/V",
            "/Ac/In/0/L1/I",
            "/Ac/In/0/L1/P",
            "/Link/ChargeVoltage",
            "/Link/ChargeCurrent",
            "/Link/BatteryVoltage",
            "/Link/BatteryCurrent",
            "/Diagnostics/Setpoint/Voltage",
            "/Diagnostics/Setpoint/Current",
            "/Diagnostics/Setpoint/Standby",
            "/Diagnostics/Can/Age",
        ):
            svc.add_path(path, None)
        svc.add_path(
            "/Ac/In/CurrentLimit",
            float(self.settings.values["ac_current_limit"]),
            writeable=True,
            onchangecallback=self.on_ac_current_limit,
        )
        svc.add_path("/Ac/In/CurrentLimitIsAdjustable", 0)
        svc.add_path("/State", STATE_OFF)
        svc.add_path("/ErrorCode", 0)
        for alarm in ("LowVoltage", "HighVoltage", "LowTemperature", "HighTemperature"):
            svc.add_path(f"/Alarms/{alarm}", 0)
        svc.add_path("/Diagnostics/Reason", "")
        svc.add_path("/Diagnostics/Can/Frames", 0)
        svc.add_path("/Diagnostics/Can/SetAcks", 0)
        svc.add_path("/Diagnostics/Can/SetErrors", 0)
        svc.add_path("/Diagnostics/Can/Errors", 0)
        if self.args.test_load is not None:
            svc.add_path("/Diagnostics/Test/SimulatedBatteryCurrent", None)
            svc.add_path("/Diagnostics/Test/Load", float(self.args.test_load))

        # Control surface.  /Mode is the switch the Venus GUI and VRM already
        # know how to draw; /Settings/ChargeMode is the three-way policy.
        svc.add_path("/Mode", VE_MODE_OFF, writeable=True, onchangecallback=self.on_ve_mode)
        svc.add_path(
            "/Settings/ChargeMode",
            int(self.settings.values["mode"]),
            writeable=True,
            onchangecallback=self.on_charge_mode,
        )
        svc.add_path(
            "/Settings/MaxCurrent",
            float(self.settings.values["max_current"]),
            writeable=True,
            onchangecallback=self.on_max_current,
        )
        svc.add_path(
            "/Settings/CompensationDelta",
            float(self.settings.values["compensation_delta"]),
            writeable=True,
            onchangecallback=self.on_compensation_delta,
        )
        svc.register()
        log.info("service D-Bus publié: %s", name)

    # -- callbacks --------------------------------------------------------

    def _store_mode(self, mode: int) -> None:
        if mode != MODE_OFF:
            self.settings.values["last_active_mode"] = mode
        self.settings.values["mode"] = mode
        self.settings.save()

    def on_charge_mode(self, _path, value):
        mode = int(value)
        if mode not in (MODE_OFF, MODE_COMPENSATION, MODE_RECHARGE):
            log.warning("mode %s inconnu, refusé", value)
            return False
        self._store_mode(mode)
        log.info("mode -> %s", self.Mode(mode).name)
        return True

    def on_max_current(self, _path, value):
        amps = float(value)
        if not 0.0 <= amps <= MAX_CURRENT_CEILING:
            log.warning("courant max %s hors bornes", value)
            return False
        self.settings.values["max_current"] = amps
        self.regulator.config.max_current_a = amps
        self.settings.save()
        return True

    def on_compensation_delta(self, _path, value):
        self.settings.values["compensation_delta"] = float(value)
        self.regulator.config.compensation_delta_v = float(value)
        self.settings.save()
        return True

    def on_ve_mode(self, _path, value):
        """0 = off, 1 = compensation, 2 = recharge.

        The three values are the three modes, so the write path is a direct
        mapping and nothing is unreachable. The old design mapped 1 to "restore
        the last active mode", which made compensation impossible to select from
        Modbus once the last active mode was recharge - found by trying it.
        /Mode is published as the real mode for the same reason: a 0/1 view
        would swallow the second write.
        """
        request = int(value)
        if request == 4:  # solarcharger convention, still written by some clients
            request = MODE_OFF
        if request not in (MODE_OFF, MODE_COMPENSATION, MODE_RECHARGE):
            log.warning("mode %s inconnu, refusé", value)
            return False
        self._store_mode(request)
        self.service["/Settings/ChargeMode"] = request
        return True

    def on_ac_current_limit(self, _path, value):
        """AC input ceiling, the only other writable register for this type.

        The R4850G2 has no AC current limit register (that is G6 only), so we
        emulate it: the ceiling is turned into a DC current cap in _dc_ceiling().
        """
        amps = float(value)
        if not 0.0 <= amps <= 63.0:
            return False
        self.settings.values["ac_current_limit"] = amps
        self.settings.save()
        return True

    def _dc_ceiling(self, battery_voltage: float | None) -> float:
        """DC amps allowed by the AC ceiling, bounded by the configured maximum."""
        configured = float(self.settings.values["max_current"])
        ac_limit = float(self.settings.values.get("ac_current_limit", 0.0))
        if ac_limit <= 0.0 or not battery_voltage:
            return configured
        ceiling = ac_limit * AC_NOMINAL_V * AC_EFFICIENCY / max(battery_voltage, 40.0)
        return min(configured, ceiling)

    # -- control loop -----------------------------------------------------

    def apply_to_module(self, setpoints, now: float | None = None) -> None:
        from r4850_can import (
            build_set_current_limit,
            build_set_fan_mode,
            build_set_standby,
            build_set_voltage,
        )

        state = self.transport.state
        if not state.nominal_current:
            log.warning("courant nominal inconnu (info 0x0001), setpoints retenus")
            return
        if now is None:
            now = time.monotonic()
        if now - self.last_apply > REWRITE_EVERY_S:
            # Cached values are no longer guaranteed to be what the module holds.
            if self.last_values:
                log.info("réécriture complète des consignes (>%.0f s)", REWRITE_EVERY_S)
            self.last_values = {}
        self.last_apply = now
        last = self.last_values
        if last.get("standby") != setpoints.standby:
            self.transport.send(*build_set_standby(setpoints.standby, self.args.address))
            last["standby"] = setpoints.standby
        if last.get("voltage") is None or abs(setpoints.voltage - last["voltage"]) >= 0.05:
            self.transport.send(*build_set_voltage(setpoints.voltage, self.args.address))
            last["voltage"] = setpoints.voltage
        if last.get("current") is None or abs(setpoints.current_limit - last["current"]) >= 0.05:
            self.transport.send(
                *build_set_current_limit(setpoints.current_limit, state.nominal_current, self.args.address)
            )
            last["current"] = setpoints.current_limit
        # Fan max above half the ceiling: the G2 has no duty-cycle register and
        # its own curve follows the *input* temperature, not the load.
        want_fan = setpoints.current_limit > 0.5 * self.regulator.config.max_current_a
        if last.get("fan") != want_fan:
            self.transport.send(*build_set_fan_mode(want_fan, self.args.address))
            last["fan"] = want_fan

    def tick(self) -> bool:
        now = time.monotonic()
        if hasattr(self.transport, "step"):
            self.transport.step()
        if now - self.last_can_tx >= 0.5:
            self.transport.request_data()
            self.last_can_tx = now
        if self.transport.state.serial is None and now - self.last_info_tx >= INFO_RETRY_S:
            self.transport.request_info()
            self.last_info_tx = now

        values = self.battery.read() if self.battery else {}
        if self.args.test_load is not None:
            # Simulated plant: the battery absorbs whatever the setpoints do not
            # consume, so the PI has something to converge on. Voltage, CVL and
            # CCL stay the real ones, only the measured current is virtual.
            values = dict(values)
            values["current"] = self.sim_battery_current
        battery = self.BatteryState(
            voltage=values.get("voltage"),
            current=values.get("current"),
            charge_voltage_limit=values.get("charge_voltage_limit"),
            charge_current_limit=values.get("charge_current_limit"),
        )
        state = self.transport.state
        can_age = state.age(now)
        can_ok = can_age <= CAN_STALE_S

        if can_ok:
            mode = self.Mode(int(self.settings.values["mode"]))
            self.regulator.config.max_current_a = self._dc_ceiling(battery.voltage)
            setpoints = self.regulator.step(mode, battery, now, battery_age_s=self._battery_age())
            self.apply_to_module(setpoints, now)
        else:
            setpoints = self.regulator.step(self.Mode.OFF, battery, now)

        if self.args.test_load is not None:
            drawn = 0.0 if setpoints.standby else setpoints.current_limit
            self.sim_battery_current = drawn - float(self.args.test_load)
        self._publish(state, battery, setpoints, can_ok, can_age)
        return True

    def _battery_age(self) -> float:
        return self.battery.age if self.battery else 0.0

    def _publish(self, state, battery, setpoints, can_ok, can_age) -> None:
        if not can_ok:
            victron_state = STATE_OFF
        elif setpoints.mode != self.Mode.OFF:
            victron_state = STATE_ABSORPTION if setpoints.standby or setpoints.current_limit <= 0.5 else STATE_BULK
        else:
            victron_state = STATE_OFF

        measured = (
            "/Dc/0/Voltage",
            "/Dc/0/Current",
            "/Dc/0/Power",
            "/Dc/0/Temperature",
            "/Ac/In/0/L1/V",
            "/Ac/In/0/L1/I",
            "/Ac/In/0/L1/P",
        )
        values = {
            "/Connected": 1 if can_ok else 0,
            "/Dc/0/Voltage": state.output_voltage,
            "/Dc/0/Current": state.output_current_fast,
            "/Dc/0/Power": state.output_power,
            "/Dc/0/Temperature": state.output_temperature,
            "/Ac/In/0/L1/V": state.input_voltage,
            "/Ac/In/0/L1/I": state.input_current,
            "/Ac/In/0/L1/P": state.input_power,
            "/State": victron_state,
            # Publish the actual mode, not a 0/1 view: a two-state register can
            # never carry a three-state command, since writing the value it
            # already holds fires no onchangecallback at all.
            "/Mode": int(setpoints.mode),
            # Never repurpose /ErrorCode: for a Victron charger, 1 means
            # "Battery temperature too high". A missing module is not a device
            # error, it is an absent device: /Connected carries that.
            "/ErrorCode": 0,
            "/Link/ChargeVoltage": battery.charge_voltage_limit,
            "/Link/ChargeCurrent": battery.charge_current_limit,
            "/Link/BatteryVoltage": battery.voltage,
            "/Link/BatteryCurrent": battery.current,
            "/Diagnostics/Setpoint/Voltage": setpoints.voltage,
            "/Diagnostics/Setpoint/Current": setpoints.current_limit,
            "/Diagnostics/Setpoint/Standby": 1 if setpoints.standby else 0,
            "/Diagnostics/Reason": setpoints.reason,
            "/Diagnostics/Can/Frames": state.frames,
            "/Diagnostics/Can/Age": round(can_age, 1) if can_ok else None,
            "/Diagnostics/Can/SetAcks": state.set_acks,
            "/Diagnostics/Can/SetErrors": state.set_errors,
            "/Diagnostics/Can/Errors": state.errors,
        }
        if self.args.test_load is not None:
            values["/Diagnostics/Test/SimulatedBatteryCurrent"] = round(self.sim_battery_current, 2)
            values["/Diagnostics/Test/Load"] = float(self.args.test_load)
        with self.service as svc:
            for path, value in values.items():
                if self.args.test and path in measured:
                    continue
                if value is not None:
                    svc[path] = value
            if not self.args.test:
                svc["/Alarms/HighVoltage"] = int(
                    bool(state.output_voltage and state.output_voltage > 58.6)
                )
                svc["/Alarms/HighTemperature"] = int(
                    bool(state.output_temperature and state.output_temperature > 75.0)
                )


    # -- lifecycle --------------------------------------------------------

    def run(self) -> int:
        self.load_modules()
        self.open_transport()
        if self.args.self_test:
            return self.run_self_test()

        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        self.battery = BatteryMonitor(self.args.battery_service)
        # The serial is part of the service name and cannot be changed once the
        # service is registered. A module that powers up with the GX answers
        # within a few seconds, so wait a little before falling back to
        # "unknown" - otherwise the device stays named .unknown until restart.
        deadline = time.monotonic() + float(self.args.serial_wait)
        while self.transport.state.serial is None and time.monotonic() < deadline:
            self.transport.request_info()
            time.sleep(1.0)
        if self.transport.state.serial:
            log.info("module détecté, série %s", self.transport.state.serial)
        else:
            log.warning(
                "aucun module après %.0f s : service enregistré sous '.unknown'",
                self.args.serial_wait,
            )
        self.create_service()
        self.mainloop = GLib.MainLoop()

        def refresh_battery():
            self.battery.refresh(self.dbus, self.VeDbusItemImport, self.dbus.SystemBus)
            return True

        refresh_battery()
        GLib.timeout_add_seconds(1, self._glib_tick)
        GLib.timeout_add_seconds(10, refresh_battery)
        signal.signal(signal.SIGTERM, lambda *_: self.mainloop.quit())
        signal.signal(signal.SIGINT, lambda *_: self.mainloop.quit())
        log.info("boucle de contrôle démarrée (1 Hz)")
        self.mainloop.run()
        self.shutdown()
        return 0

    def _glib_tick(self) -> bool:
        try:
            return self.tick()
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            log.exception("tick en échec")
            return True

    def run_self_test(self) -> int:
        """Headless: fake transport, real policy, no D-Bus, no CAN write."""
        load = 12.0  # constant DC load the rectifier must carry in compensation
        current = -load
        print(
            f"{'tick':>4}  {'mode':<12}  {'I_bat':>6}  {'V_set':>6}  {'I_set':>6}  "
            f"standby  raison"
        )
        ticks = int(self.args.self_test)
        for n in range(ticks):
            self.transport.step()
            self.transport.request_data()
            if n == 1:
                self.settings.values["mode"] = MODE_COMPENSATION
            if n == ticks // 2:
                self.settings.values["mode"] = MODE_RECHARGE
            mode = self.Mode(int(self.settings.values["mode"]))
            battery = self.BatteryState(53.0, current, 56.8, 40.0)
            sp = self.regulator.step(mode, battery, now=float(n))
            # Trivial plant: the battery absorbs whatever the load does not take.
            current = sp.current_limit - load
            print(
                f"{n:4d}  {mode.name:<12}  {battery.current:+6.1f}  "
                f"{sp.voltage:6.2f}  {sp.current_limit:6.2f}  {str(sp.standby):<7}  {sp.reason}"
            )
        self.transport.close()
        return 0

    def shutdown(self) -> None:
        log.info("arrêt demandé")
        try:
            if self.transport and self.transport.state.serial:
                from r4850_can import build_set_standby

                self.transport.send(*build_set_standby(True, self.args.address))
                log.info("module mis en standby")
        except Exception:  # noqa: BLE001
            log.exception("standby de sortie impossible")
        if self.transport:
            self.transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Huawei R4850G2 as a Victron AC charger")
    parser.add_argument("--can", default="can1", help="SocketCAN interface (default: can1)")
    parser.add_argument("--address", type=int, default=1, help="module software address")
    parser.add_argument("--device-instance", type=int, default=10)
    parser.add_argument(
        "--serial-wait",
        type=float,
        default=20.0,
        metavar="S",
        help="seconds to wait for the module serial before registering anyway "
        "(the serial is part of the service name and cannot change afterwards)",
    )
    parser.add_argument("--battery-service", default=None, help="override the BMS service name")
    parser.add_argument("--settings-file", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="fake CAN transport")
    parser.add_argument(
        "--test",
        action="store_true",
        help="virtual module: answers commands, publishes no voltage/current, writes no CAN",
    )
    parser.add_argument(
        "--test-load",
        type=float,
        metavar="A",
        help="with --test: simulated DC load, closes the loop on a virtual battery "
        "(battery current = setpoint - load). Real CVL/CCL are still used.",
    )
    parser.add_argument("--self-test", type=int, metavar="N", help="run N ticks headless and print them")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return R4850Service(args).run()


if __name__ == "__main__":
    sys.exit(main())
