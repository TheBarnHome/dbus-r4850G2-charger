#!/usr/bin/env python3
"""Huawei R4850G2 bench utility — talk to one module from a terminal.

Same codec and same transport as the D-Bus driver, so what you see here is
exactly what the service will see.  No python-can: AF_CAN only.

    ./r4850.py can1 --info                 dump the info registers (serial, nominal)
    ./r4850.py can1 --monitor 30           print telemetry for 30 s
    ./r4850.py can1 --voltage 53.5 --current 10
    ./r4850.py can1 --standby              inhibit the output
    ./r4850.py can1 --on                   re-enable the output

Values are volatile: the module forgets them ~60 s after the last frame, which
is why --monitor and the driver keep transmitting at 1 Hz.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import r4850_can as codec
from r4850_transport import CanTransport

LOG = logging.getLogger("r4850.cli")


def dump_info(transport, seconds=6.0):
    transport.request_info()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if transport.state.serial and transport.state.nominal_current:
            break
        time.sleep(0.25)
    state = transport.state
    print(f"numéro de série   : {state.serial or 'aucune réponse'}")
    if state.nominal_current:
        print(f"courant nominal   : {state.nominal_current:.1f} A")
        print(f"limite absolue    : {codec.ratio_to_amps(codec.CURRENT_RATIO_MAX, state.nominal_current):.1f} A (122 %)")
    print(f"firmware          : {state.firmware or '?'}")
    return 0 if state.serial else 1


def monitor(transport, seconds):
    print(f"{'t':>5}  {'Udc':>7}  {'Idc':>6}  {'Pdc':>7}  {'Uac':>6}  {'Tout':>5}  {'Tin':>5}  {'η':>5}")
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        transport.request_data()
        time.sleep(1.0)
        st = transport.state
        if st.output_voltage is None:
            print(f"{time.monotonic() - started:5.0f}  aucune réponse du module")
            continue
        print(
            f"{time.monotonic() - started:5.0f}  {st.output_voltage:7.2f}  {st.output_current_fast or 0:6.2f}  "
            f"{st.output_power or 0:7.1f}  {st.input_voltage or 0:6.1f}  {st.output_temperature or 0:5.1f}  "
            f"{st.input_temperature or 0:5.1f}  {(st.efficiency or 0) * 100:4.1f}%"
        )
    return 0


def apply_setpoints(args, transport):
    if not transport.state.nominal_current:
        transport.request_info()
        deadline = time.monotonic() + 5.0
        while not transport.state.nominal_current and time.monotonic() < deadline:
            time.sleep(0.25)
    if not transport.state.nominal_current:
        print("courant nominal inconnu : le module ne répond pas à l'info 0x0001", file=sys.stderr)
        return 1
    address = args.address
    if args.voltage is not None:
        transport.send(*codec.build_set_voltage(args.voltage, address))
        print(f"tension -> {args.voltage:.2f} V")
    if args.current is not None:
        transport.send(
            *codec.build_set_current_limit(args.current, transport.state.nominal_current, address)
        )
        print(f"courant -> {args.current:.2f} A (nominal {transport.state.nominal_current:.1f} A)")
    if args.standby:
        transport.send(*codec.build_set_standby(True, address))
        print("sortie -> standby")
    elif args.on:
        transport.send(*codec.build_set_standby(False, address))
        print("sortie -> active")
    if args.fan_max:
        transport.send(*codec.build_set_fan_mode(True, address))
        print("ventilateur -> vitesse max")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Huawei R4850G2 bench utility")
    parser.add_argument("interface", nargs="?", default="can1", help="SocketCAN interface")
    parser.add_argument("--address", type=int, default=1)
    parser.add_argument("-v", "--voltage", type=float, help="set output voltage (V)")
    parser.add_argument("-c", "--current", type=float, help="set current limit (A)")
    parser.add_argument("--standby", action="store_true", help="inhibit the output")
    parser.add_argument("--on", action="store_true", help="enable the output")
    parser.add_argument("--fan-max", action="store_true", help="force fan to max speed")
    parser.add_argument("--info", action="store_true", help="dump the info registers")
    parser.add_argument("--monitor", type=float, metavar="SECONDS", help="print telemetry")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    transport = CanTransport(args.interface, args.address)
    try:
        transport.open()
    except OSError as exc:
        print(f"interface {args.interface} inutilisable : {exc}", file=sys.stderr)
        return 2

    try:
        actions = [args.voltage is not None, args.current is not None, args.standby,
                   args.on, args.fan_max, args.info, args.monitor is not None]
        if not any(actions):
            transport.request_data()
            time.sleep(1.5)
            monitor(transport, 5)
            return 0
        if args.info:
            return dump_info(transport)
        if args.monitor is not None:
            return monitor(transport, args.monitor)
        return apply_setpoints(args, transport)
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
