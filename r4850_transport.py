#!/usr/bin/env python3
"""SocketCAN transport for the Huawei R48xx rectifier family.

Raw ``AF_CAN`` sockets only: no ``python-can``, which is not installed on Venus
OS and would be one more dependency to carry on a GX device.  The kernel gives
us the same frames either way; this module just packs and unpacks them and keeps
the receiver off the control loop.

Two objects:

``CanTransport``
    real bus, opened on a named interface (``can1``), with a reader thread that
    decodes every frame into a :class:`PsuState`.

``FakeTransport``
    same interface, no hardware, used by ``--dry-run`` so the D-Bus service and
    the control loop can be exercised on a target that has no rectifier wired.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

import r4850_can as can

LOG = logging.getLogger("r4850.transport")


@dataclass
class PsuState:
    """Everything the receiver learned from the module, plus its freshness."""

    serial: str | None = None
    nominal_current: float | None = None
    firmware: str | None = None
    output_voltage: float | None = None
    output_current_fast: float | None = None
    output_current_slow: float | None = None
    output_power: float | None = None
    input_voltage: float | None = None
    input_current: float | None = None
    input_power: float | None = None
    input_frequency: float | None = None
    output_temperature: float | None = None
    input_temperature: float | None = None
    efficiency: float | None = None
    alarms: int | None = None
    last_frame_at: float | None = None
    frames: int = 0
    set_acks: int = 0
    set_errors: int = 0
    errors: int = 0

    @property
    def online(self) -> bool:
        return self.last_frame_at is not None

    def age(self, now: float | None = None) -> float:
        if self.last_frame_at is None:
            return float("inf")
        return (now if now is not None else time.monotonic()) - self.last_frame_at


class CanTransport:
    """Raw SocketCAN transport with a decoding reader thread."""

    def __init__(self, interface: str = "can1", address: int = 1) -> None:
        self.interface = interface
        self.address = address
        self.state = PsuState()
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def open(self) -> None:
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        sock.bind((self.interface,))
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="r4850-rx", daemon=True)
        self._reader.start()
        LOG.info("CAN %s ouvert, adresse module %d", self.interface, self.address)

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- transmit ---------------------------------------------------------

    def send(self, can_id: int, payload: bytes) -> bool:
        if self._sock is None:
            return False
        if len(payload) != 8:
            raise ValueError(f"payload must be 8 bytes, got {len(payload)}")
        frame = struct.pack("<IB3x", can_id & 0x1FFFFFFF, 8) + payload
        try:
            self._sock.send(frame)
            return True
        except OSError as exc:
            with self._lock:
                self.state.errors += 1
            LOG.warning("envoi CAN impossible: %s", exc)
            return False

    def request_data(self) -> None:
        self.send(*can.build_data_request(self.address))

    def request_info(self) -> None:
        self.send(*can.build_info_request(self.address))

    # -- receive ----------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                raw = self._sock.recv(16)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                LOG.warning("lecture CAN interrompue: %s", exc)
                with self._lock:
                    self.state.errors += 1
                time.sleep(1.0)
                continue
            if len(raw) < 16:
                continue
            can_id, dlc = struct.unpack("<IB3x", raw[:8])
            if dlc != 8:
                continue
            self._handle(can_id, raw[8:16])

    def _handle(self, can_id: int, payload: bytes) -> None:
        parsed = can.parse_can_id(can_id)
        if parsed.to_psu:
            return
        with self._lock:
            st = self.state
            st.frames += 1
            st.last_frame_at = time.monotonic()

            if parsed.command == can.CMD_REGISTER_SET:
                frame = can.decode_register_frame(payload)
                if frame.ok:
                    st.set_acks += 1
                else:
                    st.set_errors += 1
                    LOG.warning("le module a refusé le registre 0x%04X", frame.register)
                return

            if parsed.command not in (can.CMD_DATA_REQUEST, can.CMD_INFO_REQUEST):
                return

            frame = can.decode_register_frame(payload)

            if parsed.command == can.CMD_INFO_REQUEST:
                if frame.register == can.INFO_SERIAL:
                    st.serial = can.serial_from_info(frame.raw)
                elif frame.register == can.INFO_MODULE_CHARACTERISTIC:
                    st.nominal_current = can.nominal_current_from_info(frame.raw)
                elif frame.register == can.INFO_VERSIONS:
                    versions = frame.raw.to_bytes(6, "big")
                    st.firmware = "%d.%d.%d" % (versions[2], versions[3], versions[4])
                return

            decoded = can.decode_telemetry(frame.register, frame.raw)
            if decoded is None:
                if frame.register == can.DATA_ALARMS:
                    st.alarms = frame.raw
                return
            attr, value, _unit = decoded
            setattr(st, attr, value)


class FakeTransport(CanTransport):
    """No hardware: acknowledges setpoints and, optionally, invents telemetry.

    ``telemetry=False`` is the test mode: the virtual module answers the
    commands (acks are counted, the loop runs, the policy decides against the
    real BMS), but it publishes **no** output voltage, current or temperature.
    Inventing those numbers would push fake measurements into Venus OS, VRM and
    Home Assistant history, which is worse than showing nothing.
    """

    def __init__(self, interface: str = "fake", address: int = 1, telemetry: bool = True) -> None:
        super().__init__(interface=interface, address=address)
        self.telemetry = telemetry
        self._started = 0.0
        self._injected: list[tuple[float, float]] = []  # (voltage, current) demand

    def open(self) -> None:
        self.state.serial = "test00000000" if not self.telemetry else "fake0000dead"
        self.state.nominal_current = 50.0
        self.state.firmware = "virtual"
        self._started = time.monotonic()
        LOG.info(
            "module virtuel (%s), aucun CAN réel",
            "mode test, aucune mesure publiée" if not self.telemetry else "dry-run complet",
        )

    def close(self) -> None:
        pass

    def send(self, can_id: int, payload: bytes) -> bool:
        """A virtual module acknowledges every write, and records it."""
        with self._lock:
            self.state.set_acks += 1
            self.state.last_frame_at = time.monotonic()
        return True

    def step(self) -> None:
        """Called by the control loop; fakes a module that follows its setpoints."""
        demand = 0.0
        voltage = 53.0
        elapsed = time.monotonic() - self._started
        # The fake module answers whatever the loop last asked for, with lag.
        for ts, (v, i) in self._injected:
            if ts <= elapsed:
                voltage, demand = v, i
        st = self.state
        st.last_frame_at = time.monotonic()
        st.frames += 8
        if not self.telemetry:
            return
        st.output_voltage = voltage
        st.output_current_fast = demand
        st.output_current_slow = demand
        st.output_power = voltage * demand
        st.input_voltage = 232.0
        st.input_current = (voltage * demand) / 232.0 / 0.93 if demand else 0.4
        st.input_power = voltage * demand / 0.93
        st.input_frequency = 50.0
        st.output_temperature = 34.0 + demand * 0.3
        st.input_temperature = 27.0 + demand * 0.2
        st.efficiency = 0.93
        st.alarms = 0

    def expect(self, voltage: float, current: float) -> None:
        self._injected.append((time.monotonic() - self._started + 1.0, (voltage, current)))
