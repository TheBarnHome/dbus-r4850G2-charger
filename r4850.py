#!/usr/bin/env python3
import argparse
import struct
import socket
import fcntl
import os
import time
import threading
import logging
import can

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

MAX_CURRENT = 50

# Register addresses
R48xx_DATA_INPUT_POWER = 0x70
R48xx_DATA_INPUT_FREQ = 0x71
R48xx_DATA_INPUT_CURRENT = 0x72
R48xx_DATA_OUTPUT_POWER = 0x73
R48xx_DATA_EFFICIENCY = 0x74
R48xx_DATA_OUTPUT_VOLTAGE = 0x75
R48xx_DATA_OUTPUT_CURRENT_MAX_PERCENT = 0x76
R48xx_DATA_INPUT_VOLTAGE = 0x78
R48xx_DATA_OUTPUT_TEMPERATURE = 0x7F
R48xx_DATA_INPUT_TEMPERATURE = 0x80
R48xx_DATA_OUTPUT_CURRENT = 0x81
R48xx_DATA_OUTPUT_CURRENT1 = 0x82

class RectifierParameters:
    def __init__(self):
        self.input_voltage = 0.0
        self.input_frequency = 0.0
        self.input_current = 0.0
        self.input_power = 0.0
        self.input_temp = 0.0
        self.efficiency = 0.0
        self.output_voltage = 0.0
        self.output_current = 0.0
        self.max_output_current_percent = 0.0
        self.output_power = 0.0
        self.output_temp = 0.0
        self.amp_hour = 0.0

    def print_parameters(self):
        print("\n")
        print(f"Input Voltage {self.input_voltage:.2f}V @ {self.input_frequency:.2f}Hz")
        print(f"Input Current {self.input_current:.2f}A")
        print(f"Input Power {self.input_power:.2f}W\n")
        print(f"Output Voltage {self.output_voltage:.2f}V")
        print(f"Output Current {self.output_current:.2f}A of {self.max_output_current_percent * MAX_CURRENT:.2f}A Max ({self.max_output_current_percent * 100:.1f}% of Capacity), {self.amp_hour / 3600:.3f}Ah")
        print(f"Output Power {self.output_power:.2f}W\n")
        print(f"Input Temperature {self.input_temp:.1f} DegC")
        print(f"Output Temperature {self.output_temp:.1f} DegC")
        print(f"Efficiency {self.efficiency * 100:.1f}%")

def bswap32(val):
    return struct.unpack('>I', struct.pack('<I', val))[0]

def bswap16(val):
    return struct.unpack('>H', struct.pack('<H', val))[0]

def parse_data(frame, rp):
    value = bswap32(struct.unpack('<I', bytes(frame.data[4:8]))[0]) / 1024.0
    reg = frame.data[1]

    if reg == R48xx_DATA_INPUT_POWER:
        rp.input_power = value
    elif reg == R48xx_DATA_INPUT_FREQ:
        rp.input_frequency = value
    elif reg == R48xx_DATA_INPUT_CURRENT:
        rp.input_current = value
    elif reg == R48xx_DATA_OUTPUT_POWER:
        rp.output_power = value
    elif reg == R48xx_DATA_EFFICIENCY:
        rp.efficiency = value
    elif reg == R48xx_DATA_OUTPUT_VOLTAGE:
        rp.output_voltage = value
    elif reg == R48xx_DATA_OUTPUT_CURRENT_MAX_PERCENT:
        rp.max_output_current_percent = value
    elif reg == R48xx_DATA_INPUT_VOLTAGE:
        rp.input_voltage = value
    elif reg == R48xx_DATA_OUTPUT_TEMPERATURE:
        rp.output_temp = value
    elif reg == R48xx_DATA_INPUT_TEMPERATURE:
        rp.input_temp = value
    elif reg == R48xx_DATA_OUTPUT_CURRENT:
        rp.output_current = value
        rp.print_parameters()


def parse_ack(frame):
    error = frame.data[0] & 0x20
    value = bswap32(struct.unpack('<I', bytes(frame.data[4:8]))[0]) / 1024.0
    cmd = frame.data[1]

    if cmd == 0x00:
        print(f"{'Error' if error else 'Success'} setting on-line voltage to {value:.2f}V")
    elif cmd == 0x01:
        print(f"{'Error' if error else 'Success'} setting non-volatile (off-line) voltage to {value:.2f}V")
    elif cmd == 0x03:
        print(f"{'Error' if error else 'Success'} setting on-line current to {value * MAX_CURRENT:.2f}A ({value * 100:.1f}% of Capacity)")
    elif cmd == 0x04:
        print(f"{'Error' if error else 'Success'} setting non-volatile (off-line) current to {value * MAX_CURRENT:.2f}A ({value * 100:.1f}% of Capacity)")
    else:
        print(f"{'Error' if error else 'Success'} setting unknown parameter (0x{cmd:02X})")

def request_data(bus):
    msg = can.Message(arbitration_id=0x108040FE, is_extended_id=True, data=[0]*8)
    bus.send(msg)

def set_voltage(bus, voltage, nonvolatile):
    val = int(voltage * 1024)
    cmd = 0x01 if nonvolatile else 0x00
    data = [0x01, cmd, 0x00, 0x00, 0x00, 0x00, (val >> 8) & 0xFF, val & 0xFF]
    msg = can.Message(arbitration_id=0x108180FE, is_extended_id=True, data=data)
    bus.send(msg)

def set_current(bus, current, nonvolatile):
    val = int((current / MAX_CURRENT) * 1024)
    cmd = 0x04 if nonvolatile else 0x03
    data = [0x01, cmd, 0x00, 0x00, 0x00, 0x00, (val >> 8) & 0xFF, val & 0xFF]
    msg = can.Message(arbitration_id=0x108180FE, is_extended_id=True, data=data)
    bus.send(msg)

def main():
    parser = argparse.ArgumentParser(
        description='Huawei R4850G2 CAN Configuration Utility',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('interface', help='CAN interface (e.g. can0)')
    parser.add_argument('-v', '--voltage', type=float, help='Set output voltage')
    parser.add_argument('-c', '--current', type=float, help='Set output current')
    parser.add_argument('-s', '--save', action='store_true', help='Save settings to non-volatile memory')
    parser.add_argument('-h', '--help', action='help', help='Show this help message and exit')

    args = parser.parse_args()
    
    rp = RectifierParameters()
    
    bus = can.interface.Bus(channel=args.interface, bustype='socketcan')

    if args.voltage:
        set_voltage(bus, args.voltage, False)
        if args.save:
            set_voltage(bus, args.voltage, True)

    if args.current:
        set_current(bus, args.current, False)
        if args.save:
            set_current(bus, args.current, True)

    def periodic():
        while True:
            request_data(bus)
            time.sleep(1)

    threading.Thread(target=periodic, daemon=True).start()

    for msg in bus:
        can_id = msg.arbitration_id & 0x1FFFFFFF
        if can_id == 0x1081407F:
            parse_data(msg, rp)
        elif can_id == 0x1081807E:
            parse_ack(msg)
        elif can_id == 0x1081D27F:
            print(bytes(msg.data[2:8]).decode('ascii', errors='ignore'))
        else:
            print(f"Unknown frame 0x{can_id:03X} [{msg.dlc}]", ' '.join(f"{b:02X}" for b in msg.data))

if __name__ == '__main__':
    main()
