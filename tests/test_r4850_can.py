#!/usr/bin/env python3
"""Codec tests, anchored on frames captured from real R48xx hardware."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import r4850_can as can  # noqa: E402


class CanIdTests(unittest.TestCase):
    def test_data_request_identifier(self):
        self.assertEqual(can.make_can_id(can.CMD_DATA_REQUEST), 0x108140FE)

    def test_info_request_identifier(self):
        self.assertEqual(can.make_can_id(can.CMD_INFO_REQUEST), 0x108150FE)

    def test_register_set_identifier(self):
        self.assertEqual(can.make_can_id(can.CMD_REGISTER_SET), 0x108180FE)

    def test_parse_multiframe_response(self):
        parsed = can.parse_can_id(0x1081407F)
        self.assertEqual(parsed.protocol, 0x21)
        self.assertEqual(parsed.address, 1)
        self.assertEqual(parsed.command, 0x40)
        self.assertFalse(parsed.to_psu)
        self.assertTrue(parsed.more)

    def test_parse_last_response_frame(self):
        parsed = can.parse_can_id(0x1081407E)
        self.assertFalse(parsed.to_psu)
        self.assertFalse(parsed.more)

    def test_parse_broadcast_address(self):
        self.assertEqual(can.parse_can_id(can.make_can_id(0x40, address=0)).address, 0)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            can.make_can_id(0x40, address=128)
        with self.assertRaises(ValueError):
            can.make_can_id(0x100)


class PayloadTests(unittest.TestCase):
    def test_voltage_frame_matches_documented_example(self):
        # 53.5 V * 1024 = 0xD600, register 0x0100
        can_id, payload = can.build_set_voltage(53.5)
        self.assertEqual(can_id, 0x108180FE)
        self.assertEqual(payload.hex(), "010000000000d600")

    def test_current_limit_frame_matches_documented_example(self):
        # 10 A on a 35 A nominal module -> ratio 293 = 0x0125
        can_id, payload = can.build_set_current_limit(10.0, 35.0)
        self.assertEqual(can_id, 0x108180FE)
        self.assertEqual(payload.hex(), "0103000000000125")

    def test_current_limit_is_clamped_to_can_maximum(self):
        _, payload = can.build_set_current_limit(500.0, 35.0)
        self.assertEqual(int.from_bytes(payload[2:8], "big"), 1250)

    def test_standby_and_fan_frames(self):
        self.assertEqual(can.build_set_standby(True)[1].hex(), "0132000100000000")
        self.assertEqual(can.build_set_standby(False)[1].hex(), "0132000000000000")
        self.assertEqual(can.build_set_fan_mode(True)[1].hex(), "0134000100000000")

    def test_u48_round_trip_negative(self):
        encoded = can.encode_u48(-1024)
        self.assertEqual(can.decode_u48(encoded, signed=True), -1024)


class TelemetryTests(unittest.TestCase):
    def _frame(self, hexstr):
        return can.decode_register_frame(bytes.fromhex(hexstr))

    def test_output_voltage(self):
        attr, value, unit = can.decode_telemetry(*self._reg("017500000000cdb1"))
        self.assertEqual(attr, "output_voltage")
        self.assertAlmostEqual(value, 51.42, places=2)
        self.assertEqual(unit, "V")

    def test_output_current_fast_and_slow(self):
        self.assertAlmostEqual(self._value("0181000000008c11"), 35.02, places=2)
        self.assertAlmostEqual(self._value("0182000000008c07"), 35.01, places=2)

    def test_input_voltage_and_power(self):
        self.assertAlmostEqual(self._value("0178000000038b80"), 226.875, places=3)
        self.assertAlmostEqual(self._value("01700000001cdc31"), 1847.0, delta=1.0)

    def test_temperatures(self):
        self.assertAlmostEqual(self._value("017f000000008400"), 33.0, places=3)
        self.assertAlmostEqual(self._value("0180000000006c00"), 27.0, places=3)

    def test_negative_temperature_is_signed(self):
        frame = can.decode_register_frame(bytes.fromhex("017ffffffffffc00"))
        attr, value, _ = can.decode_telemetry(frame.register, frame.raw)
        self.assertEqual(attr, "output_temperature")
        self.assertAlmostEqual(value, -1.0, places=3)

    def test_unknown_register_returns_none(self):
        self.assertIsNone(can.decode_telemetry(0x01F0, 0))

    def test_set_ack_status(self):
        ok = self._frame("0134000100000000")
        self.assertEqual(ok.register, can.REG_FAN_MODE)
        self.assertTrue(ok.ok)
        bad = self._frame("2134000100000000")
        self.assertFalse(bad.ok)

    def test_nominal_current_from_info(self):
        # R4830S1 info frame 00 01 00 00 40 46 12 67 -> 35 A
        frame = can.decode_register_frame(bytes.fromhex("0001000040461267"))
        self.assertEqual(can.nominal_current_from_info(frame.raw), 35.0)

    def test_serial_and_barcode(self):
        serial = can.decode_register_frame(bytes.fromhex("00022468 23 8c 3c 2f".replace(" ", "")))
        self.assertEqual(can.serial_from_info(serial.raw), "2468238c3c2f")
        barcode = can.decode_register_frame(bytes.fromhex("0003323130323331"))
        self.assertEqual(can.barcode_from_info(barcode.raw), "210231")

    # -- helpers ---------------------------------------------------------

    def _reg(self, hexstr):
        frame = can.decode_register_frame(bytes.fromhex(hexstr))
        return frame.register, frame.raw

    def _value(self, hexstr):
        frame = can.decode_register_frame(bytes.fromhex(hexstr))
        attr, value, _ = can.decode_telemetry(frame.register, frame.raw)
        return value


if __name__ == "__main__":
    unittest.main(verbosity=2)
