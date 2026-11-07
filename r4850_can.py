#!/usr/bin/env python3
"""Huawei R48xx rectifier family — CAN protocol codec (no I/O, pure functions).

Protocol reverse-engineered by the community and verified on R4830S1, R4850G2
and R4850G6.  Reference documentation: https://github.com/patagonaa/huawei-r48xx
(see also craigpeacock/Huawei_R4850G2_CAN).

Physical layer
--------------
    125 kbit/s, 29-bit extended identifiers, CAN_H / CAN_L plus GND.
    CAN is powered from the DC side, so the bus answers even with no AC input.

Identifier layout (29 bits, ``000a aaaa abbb bbbb cccc cccc deee eefg``)
--------------------------------------------------------------------------
    a  bits 28-23  protocol id: 0x20 = PSU <-> PSU, 0x21 = controller <-> PSU
    b  bits 22-16  software address (0 = broadcast, 1 = first PSU, ...)
    c  bits 15-8   command
    d  bit  7      source: 0 = from PSU, 1 = to PSU
    e  bits  6-2   group mask, always 0x1F
    f  bit  1      hardware/software address flag, always 1
    g  bit  0      more frames follow (multi-frame responses)

Commands
--------
    0x40  data request / data response   (telemetry, one register per frame)
    0x50  info request / info response   (serial, nominal current, versions)
    0x80  register set    / set response (write a setpoint, low nibble = status)
    0x82  register get    / get response
    0xD2  e-label request / response

Value encoding
--------------
    Every payload is a 48-bit big-endian integer in bytes 2..7.  Scaled values
    are fixed point: ``raw / 1024``.  Signed values use 48-bit two's complement.
    The current limit is a ratio of the PSU's *nominal* current (info reg 0x01),
    where 1024 == nominal and the accepted maximum is 1250 (122 %).

Safety notes that belong to the protocol, not to the caller
------------------------------------------------------------
    * Online/offline: the PSU reverts to its persistent "default" voltage and
      current limit ~60 s after the last valid CAN frame addressed to it.  A
      controller must therefore transmit continuously (this project uses 1 Hz).
    * Entering online mode resets the volatile setpoints to those defaults, so
      the first frames after a gap must be sent before trusting any reading.
    * Voltage range is 41.0-58.6 V; the persistent default voltage cannot go
      below 48.0 V.  The R4850G2 cannot power up from a default voltage below
      that, which is why this project never writes the persistent registers.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Protocol constants
# --------------------------------------------------------------------------

PROTOCOL_CONTROLLER = 0x21
GROUP_MASK = 0x1F

CMD_DATA_REQUEST = 0x40
CMD_INFO_REQUEST = 0x50
CMD_REGISTER_SET = 0x80
CMD_REGISTER_GET = 0x82
CMD_ELABEL = 0xD2

# Command and direction pairs for the frames a controller can expect.
CAN_ID_DATA_REQUEST = 0x108140FE  # controller -> PSU, address 1
CAN_ID_DATA_RESPONSE = 0x1081407F  # PSU -> controller, more frames (g = 1)
CAN_ID_DATA_RESPONSE_LAST = 0x1081407E  # PSU -> controller, last frame (g = 0)
CAN_ID_INFO_REQUEST = 0x108150FE
CAN_ID_REGISTER_SET = 0x108180FE
CAN_ID_REGISTER_SET_ACK = 0x1081807E

# Setpoint registers (command 0x80).
REG_OUTPUT_VOLTAGE = 0x0100  # V * 1024, volatile
REG_DEFAULT_VOLTAGE = 0x0101  # V * 1024, persistent — never written here
REG_OVP = 0x0102
REG_CURRENT_LIMIT = 0x0103  # ratio of nominal * 1024, volatile
REG_DEFAULT_CURRENT_LIMIT = 0x0104  # persistent — never written here
REG_AC_CURRENT_LIMIT = 0x0109  # not supported on R4850G2
REG_FAN_DUTY = 0x0114  # not supported on R4850G2
REG_STANDBY = 0x0132  # byte 2: 0x00 = on, 0x01 = standby
REG_FAN_MODE = 0x0134  # byte 2: 0x00 = auto, 0x01 = max speed (persistent)

# Telemetry registers, command 0x40 response.  Distinct names on purpose:
# 0x0175 (read output voltage) and 0x0100 (write output voltage) are different
# registers that a careless refactor will happily collapse into one.
DATA_OPERATING_HOURS = 0x010E
DATA_INPUT_POWER = 0x0170
DATA_INPUT_FREQUENCY = 0x0171
DATA_INPUT_CURRENT = 0x0172
DATA_OUTPUT_POWER = 0x0173
DATA_EFFICIENCY = 0x0174
DATA_OUTPUT_VOLTAGE = 0x0175
DATA_MAX_OUTPUT_CURRENT = 0x0176
DATA_INPUT_VOLTAGE = 0x0178
DATA_OUTPUT_TEMPERATURE = 0x017F
DATA_INPUT_TEMPERATURE = 0x0180
DATA_OUTPUT_CURRENT_FAST = 0x0181
DATA_OUTPUT_CURRENT_SLOW = 0x0182
DATA_ALARMS = 0x0183

# Info registers, command 0x50 response.
INFO_MODULE_CHARACTERISTIC = 0x0001
INFO_SERIAL = 0x0002
INFO_BARCODE_1 = 0x0003
INFO_BARCODE_2 = 0x0004
INFO_VERSIONS = 0x0005
INFO_SLOT_ID = 0x0006

# Accepted setpoint ranges (tested on R4830S1 and R4850G6).
VOLTAGE_MIN = 41.0
VOLTAGE_MAX = 58.6
CURRENT_RATIO_MAX = 1250  # 122 % of nominal

SET_STATUS_OK = 0
SET_STATUS_PARAMETER_ERROR = 2


# --------------------------------------------------------------------------
# Identifier helpers
# --------------------------------------------------------------------------


def make_can_id(
    command: int,
    address: int = 1,
    to_psu: bool = True,
    more: bool = False,
    protocol: int = PROTOCOL_CONTROLLER,
) -> int:
    """Build a 29-bit extended CAN identifier."""
    if not 0 <= address <= 0x7F:
        raise ValueError(f"address out of range: {address}")
    if not 0 <= command <= 0xFF:
        raise ValueError(f"command out of range: {command}")
    return (
        ((protocol & 0x3F) << 23)
        | ((address & 0x7F) << 16)
        | ((command & 0xFF) << 8)
        | ((1 if to_psu else 0) << 7)
        | (GROUP_MASK << 2)
        | (1 << 1)
        | (1 if more else 0)
    )


@dataclass(frozen=True)
class ParsedCanId:
    protocol: int
    address: int
    command: int
    to_psu: bool
    more: bool


def parse_can_id(can_id: int) -> ParsedCanId:
    """Decode a 29-bit extended CAN identifier."""
    return ParsedCanId(
        protocol=(can_id >> 23) & 0x3F,
        address=(can_id >> 16) & 0x7F,
        command=(can_id >> 8) & 0xFF,
        to_psu=bool((can_id >> 7) & 1),
        more=bool(can_id & 1),
    )


# --------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------


def encode_u48(value: int) -> bytes:
    if not -(1 << 47) <= value < (1 << 48):
        raise ValueError(f"value does not fit in 48 bits: {value}")
    if value < 0:
        value += 1 << 48
    return value.to_bytes(6, "big")


def decode_u48(payload: bytes, signed: bool = False) -> int:
    if len(payload) != 6:
        raise ValueError(f"expected a 6-byte payload, got {len(payload)}")
    return int.from_bytes(payload, "big", signed=signed)


def encode_register(register: int, raw: int) -> bytes:
    """Build the 8-byte payload of a u48-valued register write/read frame."""
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register out of range: {register:x}")
    return struct.pack(">H", register) + encode_u48(raw)


def encode_register_bytes(register: int, data: bytes) -> bytes:
    """Build a payload whose data field is byte-aligned, not a 48-bit integer.

    Several R48xx registers are not u48: standby and fan mode carry a single
    flag in the first data byte (``01 32 00 01 00 00 00 00``), the AC current
    limit packs a flag plus a u32, and the fan duty cycle is a u16.  Encoding
    those through encode_u48() silently sets the wrong byte.
    """
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register out of range: {register:x}")
    if len(data) > 6:
        raise ValueError(f"data does not fit in 6 bytes: {data!r}")
    return struct.pack(">H", register) + data.ljust(6, b"\x00")


# --------------------------------------------------------------------------
# Frame builders (controller -> PSU)
# --------------------------------------------------------------------------


def build_data_request(address: int = 1) -> tuple[int, bytes]:
    return make_can_id(CMD_DATA_REQUEST, address), bytes(8)


def build_info_request(address: int = 1) -> tuple[int, bytes]:
    return make_can_id(CMD_INFO_REQUEST, address), bytes(8)


def build_set_voltage(volts: float, address: int = 1) -> tuple[int, bytes]:
    """Set the *volatile* output voltage.  Caller is responsible for the range."""
    raw = int(round(volts * 1024))
    return make_can_id(CMD_REGISTER_SET, address), encode_register(REG_OUTPUT_VOLTAGE, raw)


def amps_to_ratio(amps: float, nominal_amps: float) -> int:
    """Convert an absolute current to the PSU's 1024 == nominal scale."""
    if nominal_amps <= 0:
        raise ValueError("nominal current must be positive")
    return int(round(amps / nominal_amps * 1024))


def ratio_to_amps(ratio: int, nominal_amps: float) -> float:
    return ratio / 1024.0 * nominal_amps


def build_set_current_limit(
    amps: float, nominal_amps: float, address: int = 1
) -> tuple[int, bytes]:
    """Set the *volatile* current limit, expressed in absolute amps."""
    ratio = amps_to_ratio(amps, nominal_amps)
    ratio = max(0, min(CURRENT_RATIO_MAX, ratio))
    return make_can_id(CMD_REGISTER_SET, address), encode_register(REG_CURRENT_LIMIT, ratio)


def build_set_standby(standby: bool, address: int = 1) -> tuple[int, bytes]:
    """0 = output enabled, 1 = standby (this is the clean 'off')."""
    return make_can_id(CMD_REGISTER_SET, address), encode_register_bytes(
        REG_STANDBY, bytes([0x00, 1 if standby else 0])
    )


def build_set_fan_mode(full_speed: bool, address: int = 1) -> tuple[int, bytes]:
    """Auto fan control is driven by the *input* temperature and lets the module
    run above 80 C at full load; force max speed when charging hard."""
    return make_can_id(CMD_REGISTER_SET, address), encode_register_bytes(
        REG_FAN_MODE, bytes([0x00, 1 if full_speed else 0])
    )


# --------------------------------------------------------------------------
# Response decoding (PSU -> controller)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterFrame:
    register: int
    raw: int
    status: int
    echoed: int

    @property
    def ok(self) -> bool:
        return (self.status & 0x0F) == SET_STATUS_OK


def decode_register_frame(payload: bytes) -> RegisterFrame:
    """Decode a 0x40/0x50/0x80/0x82 response payload.

    For set/get acknowledgements the low nibble of byte 0 carries the status
    (0 = ok, 2 = parameter error); for data/info responses it is part of the
    register id (always 0 in practice, registers start at 0x01xx).
    """
    if len(payload) != 8:
        raise ValueError(f"expected 8 bytes, got {len(payload)}")
    # A success acknowledgement echoes the request byte for byte (first byte
    # 0x01); a failure raises bit 5, so an error frame starts with 0x21.  The
    # status is therefore the high nibble, and the register id keeps the low
    # nibble of that first byte.
    register = ((payload[0] & 0x0F) << 8) | payload[1]
    raw = decode_u48(payload[2:8])
    return RegisterFrame(register=register, raw=raw, status=payload[0] >> 4, echoed=payload[0])


# register -> (attribute, divisor, signed, unit)
_TELEMETRY: dict[int, tuple[str, float, bool, str]] = {
    DATA_OPERATING_HOURS: ("operating_hours", 1024.0, False, "h"),
    DATA_INPUT_POWER: ("input_power", 1024.0, False, "W"),
    DATA_INPUT_FREQUENCY: ("input_frequency", 1024.0, False, "Hz"),
    DATA_INPUT_CURRENT: ("input_current", 1024.0, False, "A"),
    DATA_OUTPUT_POWER: ("output_power", 1024.0, False, "W"),
    DATA_EFFICIENCY: ("efficiency", 1024.0, False, ""),
    DATA_OUTPUT_VOLTAGE: ("output_voltage", 1024.0, False, "V"),
    DATA_MAX_OUTPUT_CURRENT: ("max_output_current_ratio", 1024.0, False, ""),
    DATA_INPUT_VOLTAGE: ("input_voltage", 1024.0, False, "V"),
    DATA_OUTPUT_TEMPERATURE: ("output_temperature", 1024.0, True, "C"),
    DATA_INPUT_TEMPERATURE: ("input_temperature", 1024.0, True, "C"),
    DATA_OUTPUT_CURRENT_FAST: ("output_current_fast", 1024.0, False, "A"),
    DATA_OUTPUT_CURRENT_SLOW: ("output_current_slow", 1024.0, False, "A"),
}


def decode_telemetry(register: int, raw: int) -> tuple[str, float, str] | None:
    """Map a data register to (attribute, physical value, unit)."""
    spec = _TELEMETRY.get(register)
    if spec is None:
        return None
    attr, divisor, signed, unit = spec
    value = raw / divisor
    if signed and raw >= (1 << 47):
        value = (raw - (1 << 48)) / divisor
    return attr, value, unit


def nominal_current_from_info(raw: int) -> float:
    """Info register 0x0001 carries the nominal current.

    The field sits in the middle bytes of the data word, not in the low bits:
    ``00 01 | 00 00 40 46 | 12 67`` is a 35 A module -> (0x4046 & 0x3FF) / 2.
    """
    return ((raw >> 16) & 0x3FF) / 2.0


def serial_from_info(raw: int) -> str:
    return f"{raw:012x}"


def barcode_from_info(raw: int) -> str:
    return raw.to_bytes(6, "big").decode("ascii", errors="replace").strip("\x00")
