#!/usr/bin/env python3
"""Huawei R4850G2 as a Victron AC charger on the DC bus.

D-Bus service ``com.victronenergy.charger.huawei-r4850g2.<serial>`` fed by the
rectifier's own CAN telemetry, with the BMS as the authority on charge voltage
and current.  Three modes, one control loop at 1 Hz.

    off           output inhibited (standby register), current limit zero
    compensation  rectifier carries the load, battery held at 0 A
    recharge      full rectifier power up to the BMS CVL

Everything that can hurt the battery lives in ``r4850_modes`` (pure, tested).
This file is the I/O shell: SocketCAN, D-Bus, files, MQTT, the clock.

Usage
-----
    ./dbus-r4850-ac-charger.py --can can1 --device-instance 10
    ./dbus-r4850-ac-charger.py --self-test 10 --no-mqtt   # headless, no D-Bus
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

# Victron /Mode values (the plain on/off switch, not our three modes).
VE_MODE_ON = 1
VE_MODE_OFF = 4

CAN_STALE_S = 5.0
BATTERY_STALE_S = 12.0
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
# MQTT: Home Assistant discovery + commands
# ---------------------------------------------------------------------------


class MqttBridge:
    """Minimal HA bridge: discovery, state, availability, command topics.

    The GX publishes nothing usable to a home broker on its own — the only
    bridge on this system is ``dbus-mqtt-integrations``, which speaks VRM, not
    Home Assistant discovery (measured: one topic in 12 s).  So the driver
    carries its own bridge.  An unreachable broker must never stop the charger,
    hence the guarded constructor at the call site.
    """

    def __init__(self, host, port, prefix, node, name, on_command) -> None:
        import paho.mqtt.client as mqtt

        self.prefix = prefix.strip("/")
        self.node = node
        self.name = name
        self.on_command = on_command
        self.online = False
        self.root = f"{self.prefix}/r4850/{node}"
        api = getattr(mqtt, "CallbackAPIVersion", None)
        self.client = (
            mqtt.Client(api.VERSION2, f"r4850-{node}") if api else mqtt.Client(f"r4850-{node}")
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.will_set(f"{self.root}/availability", "offline", retain=True)
        self.client.connect(host, port, 30)
        self.client.loop_start()
        log.info("MQTT %s:%d, racine %s", host, port, self.root)

    # -- topics -----------------------------------------------------------

    def topic(self, suffix: str) -> str:
        return f"{self.root}/{suffix}"

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        failed = getattr(rc, "is_failure", None)
        if failed is True or (failed is None and isinstance(rc, int) and rc != 0):
            log.error("MQTT refusé: %s", rc)
            return
        self.online = True
        client.publish(f"{self.root}/availability", "online", retain=True)
        for suffix in ("mode/set", "max_current/set", "compensation_delta/set"):
            client.subscribe(self.topic(suffix))
        for topic, payload in self._discovery():
            client.publish(topic, json.dumps(payload), retain=True)
        log.info("MQTT prêt, discovery publiée")

    def _on_message(self, client, userdata, msg) -> None:
        suffix = msg.topic[len(self.root) + 1 :]
        try:
            payload = msg.payload.decode().strip()
        except UnicodeDecodeError:
            return
        try:
            self.on_command(suffix, payload)
        except Exception:  # noqa: BLE001
            log.exception("commande MQTT %s=%s refusée", suffix, payload)

    # -- discovery --------------------------------------------------------

    def _device(self) -> dict:
        return {
            "identifiers": [f"r4850_{self.node}"],
            "name": self.name,
            "manufacturer": "Huawei",
            "model": "R4850G2",
        }

    def _discovery(self):
        device = self._device()
        avail = f"{self.root}/availability"
        base = dict(device=device, availability_topic=avail)

        yield (
            f"homeassistant/select/r4850_{self.node}/mode/config",
            dict(
                name="Mode",
                command_topic=self.topic("mode/set"),
                state_topic=self.topic("mode"),
                options=["off", "compensation", "recharge"],
                icon="mdi:battery-charging",
                unique_id=f"r4850_{self.node}_mode",
                **base,
            ),
        )
        for key, name, unit, dev_class, low, high, step in (
            ("max_current", "Courant maximum", "A", "current", 0, 60, 0.5),
            ("compensation_delta", "Delta de compensation", "V", "voltage", 0.1, 2.0, 0.1),
        ):
            yield (
                f"homeassistant/number/r4850_{self.node}/{key}/config",
                dict(
                    name=name,
                    command_topic=self.topic(f"{key}/set"),
                    state_topic=self.topic(key),
                    min=low,
                    max=high,
                    step=step,
                    unit_of_measurement=unit,
                    device_class=dev_class,
                    unique_id=f"r4850_{self.node}_{key}",
                    **base,
                ),
            )
        sensors = (
            ("dc_voltage", "Tension sortie", "V", "voltage"),
            ("dc_current", "Courant sortie", "A", "current"),
            ("dc_power", "Puissance sortie", "W", "power"),
            ("ac_power", "Puissance secteur", "W", "power"),
            ("output_temperature", "Température sortie", "°C", "temperature"),
            ("input_temperature", "Température entrée", "°C", "temperature"),
            ("battery_current", "Courant batterie", "A", "current"),
            ("setpoint_current", "Consigne courant", "A", "current"),
            ("setpoint_voltage", "Consigne tension", "V", "voltage"),
        )
        for key, name, unit, dev_class in sensors:
            yield (
                f"homeassistant/sensor/r4850_{self.node}/{key}/config",
                dict(
                    name=name,
                    state_topic=self.topic(key),
                    unit_of_measurement=unit,
                    device_class=dev_class,
                    state_class="measurement",
                    unique_id=f"r4850_{self.node}_{key}",
                    **base,
                ),
            )

    # -- publish ----------------------------------------------------------

    def publish(self, **values) -> None:
        if not self.online:
            return
        for key, value in values.items():
            if value is None:
                continue
            self.client.publish(
                self.topic(key), f"{value:.2f}" if isinstance(value, float) else str(value)
            )

    def stop(self) -> None:
        if self.online:
            self.client.publish(f"{self.root}/availability", "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()


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
            },
        )
        self.mqtt = None
        self.transport = None
        self.regulator = None
        self.battery = None
        self.last_can_tx = 0.0
        self.last_values: dict = {}
        self.info_deadline = time.monotonic() + 15.0

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
        if self.args.dry_run or self.args.self_test:
            from r4850_transport import FakeTransport

            self.transport = FakeTransport()
        else:
            from r4850_transport import CanTransport

            self.transport = CanTransport(self.args.can, self.args.address)
        self.transport.open()
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
        svc.add_path("/Ac/In/CurrentLimit", 16.0)
        svc.add_path("/Ac/In/CurrentLimitIsAdjustable", 0)
        svc.add_path("/State", STATE_OFF)
        svc.add_path("/ErrorCode", 0)
        for alarm in ("LowVoltage", "HighVoltage", "LowTemperature", "HighTemperature"):
            svc.add_path(f"/Alarms/{alarm}", 0)
        svc.add_path("/Diagnostics/Reason", "")
        svc.add_path("/Diagnostics/Can/Frames", 0)
        svc.add_path("/Diagnostics/Can/SetAcks", 0)
        svc.add_path("/Diagnostics/Can/SetErrors", 0)

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
        if int(value) == VE_MODE_OFF:
            self._store_mode(MODE_OFF)
            self.service["/Settings/ChargeMode"] = MODE_OFF
            return True
        saved = int(self.settings.values.get("last_active_mode", MODE_COMPENSATION))
        restore = saved if saved != MODE_OFF else MODE_COMPENSATION
        self._store_mode(restore)
        self.service["/Settings/ChargeMode"] = restore
        return True

    def on_mqtt_command(self, suffix: str, payload: str) -> None:
        if suffix == "mode/set":
            mapping = {"off": MODE_OFF, "compensation": MODE_COMPENSATION, "recharge": MODE_RECHARGE}
            mode = mapping.get(payload.lower())
            if mode is None:
                log.warning("mode MQTT inconnu: %r", payload)
                return
            if self.on_charge_mode(None, mode):
                self.service["/Settings/ChargeMode"] = mode
        elif suffix == "max_current/set":
            amps = float(payload)
            if self.on_max_current(None, amps):
                self.service["/Settings/MaxCurrent"] = amps
        elif suffix == "compensation_delta/set":
            volts = float(payload)
            if self.on_compensation_delta(None, volts):
                self.service["/Settings/CompensationDelta"] = volts

    # -- control loop -----------------------------------------------------

    def apply_to_module(self, setpoints) -> None:
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
        if self.transport.state.serial is None and now < self.info_deadline:
            self.transport.request_info()

        values = self.battery.read() if self.battery else {}
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
            setpoints = self.regulator.step(mode, battery, now, battery_age_s=self._battery_age())
            self.apply_to_module(setpoints)
        else:
            setpoints = self.regulator.step(self.Mode.OFF, battery, now)

        self._publish(state, battery, setpoints, can_ok, can_age)
        return True

    def _battery_age(self) -> float:
        return self.battery.age if self.battery else 0.0

    def _publish(self, state, battery, setpoints, can_ok, can_age) -> None:
        if not can_ok:
            victron_state = STATE_FAULT
        elif setpoints.mode != self.Mode.OFF:
            victron_state = STATE_ABSORPTION if setpoints.standby or setpoints.current_limit <= 0.5 else STATE_BULK
        else:
            victron_state = STATE_OFF

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
            "/Mode": VE_MODE_OFF if setpoints.mode == self.Mode.OFF else VE_MODE_ON,
            "/ErrorCode": 0 if can_ok else 1,
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
        }
        with self.service as svc:
            for path, value in values.items():
                if value is not None:
                    svc[path] = value
            svc["/Alarms/HighVoltage"] = int(bool(state.output_voltage and state.output_voltage > 58.6))
            svc["/Alarms/HighTemperature"] = int(
                bool(state.output_temperature and state.output_temperature > 75.0)
            )

        if self.mqtt:
            self.mqtt.publish(
                mode={0: "off", 1: "compensation", 2: "recharge"}[int(setpoints.mode)],
                max_current=round(self.regulator.config.max_current_a, 1),
                compensation_delta=round(self.regulator.config.compensation_delta_v, 2),
                dc_voltage=state.output_voltage,
                dc_current=state.output_current_fast,
                dc_power=state.output_power,
                ac_power=state.input_power,
                output_temperature=state.output_temperature,
                input_temperature=state.input_temperature,
                battery_current=battery.current,
                setpoint_current=setpoints.current_limit,
                setpoint_voltage=setpoints.voltage,
            )

    # -- lifecycle --------------------------------------------------------

    def start_mqtt(self) -> None:
        if self.args.no_mqtt:
            return
        node = self.transport.state.serial or self.args.can
        try:
            self.mqtt = MqttBridge(
                self.args.mqtt_host,
                self.args.mqtt_port,
                self.args.mqtt_prefix,
                node,
                f"Huawei R4850G2 ({self.args.can})",
                self.on_mqtt_command,
            )
        except Exception:  # noqa: BLE001 - an unreachable broker must not stop the charger
            log.exception("MQTT indisponible, le service continue sans Home Assistant")
            self.mqtt = None

    def run(self) -> int:
        self.load_modules()
        self.open_transport()
        if self.args.self_test:
            return self.run_self_test()

        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        self.battery = BatteryMonitor(self.args.battery_service)
        self.create_service()
        self.start_mqtt()
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
        """Headless: fake transport, real policy, no D-Bus, no MQTT, no CAN write."""
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
        if self.mqtt:
            self.mqtt.stop()
        if self.transport:
            self.transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Huawei R4850G2 as a Victron AC charger")
    parser.add_argument("--can", default="can1", help="SocketCAN interface (default: can1)")
    parser.add_argument("--address", type=int, default=1, help="module software address")
    parser.add_argument("--device-instance", type=int, default=10)
    parser.add_argument("--battery-service", default=None, help="override the BMS service name")
    parser.add_argument("--settings-file", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="fake CAN transport")
    parser.add_argument("--self-test", type=int, metavar="N", help="run N ticks headless and print them")
    parser.add_argument("--no-mqtt", action="store_true")
    parser.add_argument("--mqtt-host", default="192.168.10.100")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-prefix", default="homeassistant")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return R4850Service(args).run()


if __name__ == "__main__":
    sys.exit(main())
