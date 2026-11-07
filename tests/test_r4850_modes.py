#!/usr/bin/env python3
"""Behaviour tests for the setpoint policy.

The plant model used here is deliberately trivial: the rectifier delivers the
current limit it is given, the battery absorbs whatever the load does not take
(``i_bat = i_psu - i_load``, positive = charging).  That is enough to prove the
properties that matter: convergence, the voltage ceiling, the fail-safe paths
and the asymmetry between ramping up and cutting down.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from r4850_can import VOLTAGE_MAX, VOLTAGE_MIN  # noqa: E402
from r4850_modes import (  # noqa: E402
    BatteryState,
    Mode,
    Regulator,
    RegulatorConfig,
)


def run(regulator, mode, battery_at, ticks, load_at, start_voltage=53.0):
    """Simulate ``ticks`` control periods; ``load_at(t)`` returns the DC load."""
    voltage = start_voltage
    current = -load_at(0)
    trace = []
    for tick in range(ticks):
        setpoints = regulator.step(
            mode, battery_at(current), now=float(tick)
        )
        voltage = setpoints.voltage
        current = setpoints.current_limit - load_at(tick)
        trace.append((tick, setpoints, current))
    return trace


class OffModeTests(unittest.TestCase):
    def test_off_inhibits_output(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.OFF, BatteryState(53.0, -4.0, 56.8, 30.0), now=0.0)
        self.assertTrue(setpoints.standby)
        self.assertEqual(setpoints.current_limit, 0.0)

    def test_off_keeps_a_safe_voltage(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.OFF, BatteryState(53.0, -4.0, 56.8, 30.0), now=0.0)
        self.assertAlmostEqual(setpoints.voltage, 53.0, places=2)


class CompensationTests(unittest.TestCase):
    def test_missing_battery_current_is_fail_safe(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(
            Mode.COMPENSATION, BatteryState(voltage=53.0, current=None), now=0.0
        )
        self.assertTrue(setpoints.standby)
        self.assertEqual(setpoints.current_limit, 0.0)

    def test_stale_battery_data_is_fail_safe(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(
            Mode.COMPENSATION,
            BatteryState(53.0, -5.0, 56.8, 30.0),
            now=0.0,
            battery_age_s=42.0,
        )
        self.assertTrue(setpoints.standby)
        self.assertEqual(setpoints.current_limit, 0.0)

    def test_offset_is_applied_on_top_of_battery_voltage(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.COMPENSATION, BatteryState(53.0, 0.0, 56.8, 30.0), now=0.0)
        self.assertAlmostEqual(setpoints.voltage, 53.5, places=2)

    def test_ceiling_is_the_bms_limit_not_the_battery_voltage(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.COMPENSATION, BatteryState(58.0, 0.0, 56.8, 30.0), now=0.0)
        self.assertAlmostEqual(setpoints.voltage, 56.8, places=2)

    def test_converges_to_zero_battery_current(self):
        regulator = Regulator(RegulatorConfig())
        trace = run(
            regulator,
            Mode.COMPENSATION,
            lambda i: BatteryState(53.0, i, 56.8, 30.0),
            ticks=25,
            load_at=lambda tick: 10.0,
        )
        settled = [abs(current) for _, _, current in trace if current is not None]
        self.assertLess(
            settled[-1], 1.0, f"battery current did not settle: {settled[-6:]}"
        )
        # Never overshoot the load while ramping up.
        self.assertLessEqual(max(s.current_limit for _, s, _ in trace), 10.0 + 1e-6)

    def test_cuts_down_within_one_control_period(self):
        """A collapse of the load cannot be seen before the next battery sample.

        At 1 Hz the battery absorbs the surplus for exactly one period, bounded
        by the voltage ceiling (battery voltage + compensation_delta_v), and the
        asymmetric slew removes it on the following tick.  Asserting anything
        stricter would be asserting against physics.
        """
        regulator = Regulator(RegulatorConfig())
        trace = run(
            regulator,
            Mode.COMPENSATION,
            lambda i: BatteryState(53.0, i, 56.8, 30.0),
            ticks=15,
            load_at=lambda tick: 20.0 if tick < 10 else 2.0,
        )
        first_tick_after_drop = trace[10][2]
        recovered = trace[11][2]
        self.assertLess(
            abs(recovered), 2.0, f"still {recovered:+.1f} A one tick later"
        )
        self.assertLess(first_tick_after_drop, 25.0)

    def test_current_limit_is_capped(self):
        config = RegulatorConfig(max_current_a=12.0)
        regulator = Regulator(config)
        trace = run(
            regulator,
            Mode.COMPENSATION,
            lambda i: BatteryState(53.0, i, 56.8, 80.0),
            ticks=20,
            load_at=lambda tick: 30.0,
        )
        self.assertLessEqual(max(s.current_limit for _, s, _ in trace), 12.0 + 1e-6)


class RechargeTests(unittest.TestCase):
    def test_refuses_to_charge_without_cvl(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.RECHARGE, BatteryState(53.0, 0.0, None, None), now=0.0)
        self.assertTrue(setpoints.standby)
        self.assertEqual(setpoints.current_limit, 0.0)
        self.assertIn("CVL", setpoints.reason)

    def test_uses_cvl_as_voltage_and_ccl_as_current(self):
        regulator = Regulator(RegulatorConfig())
        trace = run(
            regulator,
            Mode.RECHARGE,
            lambda i: BatteryState(52.0, i, 56.8, 30.0),
            ticks=12,
            load_at=lambda tick: 0.0,
            start_voltage=52.0,
        )
        last = trace[-1][1]
        self.assertAlmostEqual(last.voltage, 56.8, places=2)
        self.assertAlmostEqual(last.current_limit, 30.0, places=2)

    def test_ccl_wins_over_the_configured_maximum(self):
        regulator = Regulator(RegulatorConfig(max_current_a=50.0))
        trace = run(
            regulator,
            Mode.RECHARGE,
            lambda i: BatteryState(52.0, i, 56.8, 18.0),
            ticks=12,
            load_at=lambda tick: 0.0,
        )
        self.assertAlmostEqual(trace[-1][1].current_limit, 18.0, places=2)

    def test_holds_current_at_the_voltage_limit(self):
        regulator = Regulator(RegulatorConfig())
        setpoints = regulator.step(Mode.RECHARGE, BatteryState(56.9, 0.0, 56.8, 30.0), now=0.0)
        self.assertEqual(setpoints.current_limit, 0.0)
        self.assertAlmostEqual(setpoints.voltage, 56.8, places=2)


class RangeTests(unittest.TestCase):
    def test_voltage_never_leaves_the_psu_window(self):
        regulator = Regulator(RegulatorConfig())
        low = regulator.step(Mode.RECHARGE, BatteryState(30.0, 0.0, 30.0, 10.0), now=0.0)
        self.assertGreaterEqual(low.voltage, VOLTAGE_MIN)
        high = regulator.step(Mode.RECHARGE, BatteryState(30.0, 0.0, 70.0, 10.0), now=1.0)
        self.assertLessEqual(high.voltage, VOLTAGE_MAX)

    def test_voltage_never_exceeds_the_bms_limit(self):
        regulator = Regulator(RegulatorConfig())
        for cvl in (42.0, 53.2, 55.0, 56.8, 58.6):
            setpoints = regulator.step(
                Mode.COMPENSATION, BatteryState(57.0, 0.0, cvl, 20.0), now=float(cvl)
            )
            self.assertLessEqual(setpoints.voltage, min(cvl, VOLTAGE_MAX) + 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
