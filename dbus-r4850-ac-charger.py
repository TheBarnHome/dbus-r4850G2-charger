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
import datetime
import dbus

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService  # noqa: E402

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
    def __init__(self, productname='R4850G2', connection='Huawei R4850G2 interface', deviceinstance=0):

        self._queued_updates = []
        
        # Create the services
        self._dbuscharger = VeDbusService(f'com.victronenergy.charger.r4850.{deviceinstance}', bus=dbusconnection(), register=False)

        # Set up default paths
        self.setupChargerDefaultPaths(self._dbuscharger, connection, deviceinstance, f"Charger {productname}")

        # Create paths for charger
        # general data
        self._dbuscharger.add_path('/Ac/In/L1/I', 0)
        self._dbuscharger.add_path('/Ac/In/L1/P', 0)
        self._dbuscharger.add_path('/Ac/In/CurrentLimit', 0)
        self._dbuscharger.add_path('/NrOfOutputs', 1)
        self._dbuscharger.add_path('/DC/0/Temperature', 123)
        self._dbuscharger.add_path('/Dc/0/Voltage', 0)
        self._dbuscharger.add_path('/Dc/0/Current', 0)
        self._dbuscharger.add_path('/State', 0)
        self._dbuscharger.add_path('/Mode', 0)
        self._dbuscharger.add_path('/ErrorCode', 0)
        self._dbuscharger.add_path('/Alarms/LowVoltage', 0)
        self._dbuscharger.add_path('/Alarms/HighVoltage', 0)

        logging.info(f"Paths for 'accharger' created.")

        self._dbuscharger.register()

        logging.info(f'Added to D-Bus: {self._dbuscharger}')

        GLib.timeout_add(self.updateInterval, self._update)

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

    def _change(self, path, value):
        global mainloop
        logging.info("updated %s to %s" % (path, value))
        if path == '/Settings/Reset':
            logging.info("Restarting!")
            mainloop.quit()
            exit

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--can","-c", required=True, type=str)
    global args
    args = parser.parse_args()

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    mppservice = DbusR4850Service()
    logging.info('Created service & connected to dbus, switching over to GLib.MainLoop() (= event based)')

    global mainloop

    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()