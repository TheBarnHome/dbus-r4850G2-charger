#!/usr/bin/env python3

"""
Handle automatic connection with Huawei R48XX compatible device
This will output 1 ac charger dbus services for data and control
via VRM of the features.
"""
VERSION = 'v0.1' 

from gi.repository import GLib
import platform
import argparse
import logging
import sys
import os
import json
from enum import Enum
import datetime
import dbus
import dbus.service
import subprocess
import time
import atexit
import concurrent.futures
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))
from vedbus import VeDbusService, VeDbusItemExport, VeDbusItemImport

def find_battery_service():
    bus = dbus.SystemBus()
    om = bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus')
    iface = dbus.Interface(om, 'org.freedesktop.DBus')
    names = iface.ListNames()

    for name in names:
        if name.startswith('com.victronenergy.battery.'):
            return name
    return None

def isNaN(num):
    return num != num

def adjust_charge_current(c, battery_current):
    """
    Adjusts the charge current setpoint in c['/Dc/0/Current'] to compensate battery discharge.

    Parameters:
    - c: dict-like object containing the '/Dc/0/Current' setpoint (integer)
    - battery_current: object with get_value() returning current battery current (float),
                      negative when discharging, positive when charging.

    The function accumulates compensation when discharge continues,
    favors a slight charge over discharge,
    and gradually reduces charge when battery is not discharging.
    """

    measured_current = battery_current.get_value()  # measured current (negative = discharging)
    current_setpoint = c.get('/Dc/0/Current', 0)    # current charge setpoint (int)

    if measured_current < 0:
        # Battery discharging: increase compensation by adding absolute discharge current
        # Round up fractional values to ensure slight overcompensation
        discharge_to_compensate = int(abs(measured_current)) + (1 if abs(measured_current) % 1 > 0 else 0)

        # Add discharge to current compensation setpoint
        new_setpoint = current_setpoint + discharge_to_compensate
    else:
        # Battery charging or neutral: decrease compensation gradually to avoid oscillations
        new_setpoint = max(0, current_setpoint - 1)

    c['/Dc/0/Current'] = new_setpoint

# Allow to have multiple DBUS connections
class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM) 
class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)
def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

class DbusR4850Service(object):
    def __init__(self, productname='R4850G2', connection='Huawei R4850G2 interface', deviceinstance=10):

        self._queued_updates = []
        
        # Create the services
        self._dbuscharger = VeDbusService(f'com.victronenergy.charger.r4850', bus=dbusconnection(), register=False)

        # Set up default paths
        self.setupChargerDefaultPaths(self._dbuscharger, connection, deviceinstance, f"Charger {productname}")

        # Create paths for charger
        # general data
        self._dbuscharger.add_path('/Ac/In/L1/I', 3)
        self._dbuscharger.add_path('/Ac/In/L1/P', 600)
        self._dbuscharger.add_path('/Ac/In/CurrentLimit', 16)
        self._dbuscharger.add_path('/NrOfOutputs', 1)
        self._dbuscharger.add_path('/Dc/0/Temperature', 123)
        self._dbuscharger.add_path('/Dc/0/Voltage', 0)
        self._dbuscharger.add_path('/Dc/0/Current', 10)
        self._dbuscharger.add_path('/State', 3)
        self._dbuscharger.add_path('/Mode', 1)
        self._dbuscharger.add_path('/ErrorCode', 0)
        self._dbuscharger.add_path('/Alarms/LowVoltage', 0)
        self._dbuscharger.add_path('/Alarms/HighVoltage', 0)
        self._dbuscharger.add_path('/Relay/0/State', 0)

        logging.info(f"Paths for 'accharger' created.")

        self._dbuscharger.register()

        logging.info(f'Added to D-Bus: {self._dbuscharger}')

        GLib.timeout_add(3000, self._update)

    def setupChargerDefaultPaths(self, service, connection, deviceinstance, productname):
        # Create the management objects, as specified in the ccgx dbus-api document
        service.add_path('/Mgmt/ProcessName', __file__)
        service.add_path('/Mgmt/ProcessVersion', 'version f{VERSION}, and running on Python ' + platform.python_version())
        service.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        service.add_path('/DeviceInstance', deviceinstance)
        service.add_path('/ProductId', None)
        service.add_path('/ProductName', productname)
        service.add_path('/FirmwareVersion', None)
        service.add_path('/HardwareVersion', None)
        service.add_path('/Connected', 1)

        # Create the paths for modifying the system manually
        service.add_path('/Settings/Reset', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Charger', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Output', None, writeable=True, onchangecallback=self._change)

    def _updateInternal(self):
        # Store in the paths all values that were updated from _handleChangedValue
        with self._dbuscharger as m:
            for path, value, in self._queued_updates:
                m[path] = value
            self._queued_updates = []

    def _update(self):
        global mainloop
        logging.info("{} updating".format(datetime.datetime.now().time()))

        battery_service = find_battery_service()

        if battery_service:
            battery_voltage = VeDbusItemImport(dbusconnection(), battery_service, '/Dc/0/Voltage')
            battery_current = VeDbusItemImport(dbusconnection(), battery_service, '/Dc/0/Current')
            battery_temperature = VeDbusItemImport(dbusconnection(), battery_service, '/Dc/0/Temperature')

            with self._dbuscharger as c:
                c['/Dc/0/Voltage'] = round(battery_voltage.get_value(), 1)
                c['/Dc/0/Temperature'] = battery_temperature.get_value()
                if c['/Relay/0/State'] == 1:
                    logging.WARNING('/Relay/0/State set to 1')
                    adjust_charge_current(c, battery_voltage.get_value())
        
        with self._dbuscharger as c:
            logging.info(c)
            for path, value, in c:
                mqtt_pub.publish_sensor(path, value)

        self._updateInternal()
        return True

    def _change(self, path, value):
        global mainloop
        logging.info("updated %s to %s" % (path, value))
        if path == '/Settings/Reset':
            logging.info("Restarting!")
            mainloop.quit()
            exit

def slugify(path):
    return path.strip('/').replace('/', '_').replace(' ', '_')

class MqttPublisher:
    def __init__(self, client_id='r4850g2', host='localhost', port=1883, base_topic='homeassistant'):
        self.client = mqtt.Client(client_id)
        self.client.connect(host, port, 60)
        self.base_topic = base_topic
        self.device_id = client_id
        self.client.loop_start()

    def publish_sensor(self, path, value, unit=None, device_class=None, state_class=None):
        sensor_id = slugify(path)
        state_topic = f"{self.base_topic}/sensor/{self.device_id}/{sensor_id}/state"
        config_topic = f"{self.base_topic}/sensor/{self.device_id}/{sensor_id}/config"

        config_payload = {
            "name": f"R4850G2 {sensor_id}",
            "state_topic": state_topic,
            "unique_id": f"{self.device_id}_{sensor_id}",
            "device": {
                "identifiers": [self.device_id],
                "name": "Huawei R4850G2 Charger",
                "model": "R4850G2",
                "manufacturer": "Huawei"
            }
        }

        if unit: config_payload["unit_of_measurement"] = unit
        if device_class: config_payload["device_class"] = device_class
        if state_class: config_payload["state_class"] = state_class

        self.client.publish(config_topic, json.dumps(config_payload), retain=True)
        self.client.publish(state_topic, str(value), retain=True)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--can","-c", required=True, type=str)
    global args
    args = parser.parse_args()

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    mppservice = DbusR4850Service()
    mqtt_pub = MqttPublisher(host='192.168.10.100')

    logging.info('Created service & connected to dbus, switching over to GLib.MainLoop() (= event based)')

    global mainloop

    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()