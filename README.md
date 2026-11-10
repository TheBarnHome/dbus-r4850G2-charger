# dbus-r4850G2-charger for Venus OS

Drive a **Huawei R4850G2** rectifier from **Venus OS** over CAN, as an AC charger
on a Victron DC bus. The rectifier has three modes: **off**, **compensation**
and **recharge**. Voltage always follows the voltage the battery asks for.

Sibling project: [dbus-mpp-p18](https://github.com/TheBarnHome/dbus-mpp-p18),
same conventions (D-Bus service on Venus OS, serial-indexed service names,
`/data/etc` install, persistent state outside the Git checkout).

---

## Why a rectifier on the DC bus

The R4850G2 is a 3000 W, 42-58 V, ~50 A AC/DC rectifier with a CAN port. Wired
to the battery bus next to the BMS and the hybrid inverters, it turns grid power
into DC power under software control. Two uses:

- **compensation**: when the battery discharges, lift the rectifier so it
  carries the load and the pack stays at 0 A. The pack stops cycling for
  nothing, and the energy comes from the grid instead of the battery.
- **recharge**: when the pack must be refilled, push full rectifier power,
  bounded by what the BMS allows.

The BMS stays the authority: it owns the charge voltage limit (CVL) and the
charge current limit (CCL), and this driver never exceeds either.

---

## Modes

| Mode | Voltage setpoint | Current limit | Standby |
|---|---|---|---|
| `off` | safe value (battery or CVL, clamped) | 0 A | **on** |
| `compensation` | `min(CVL, V_battery + delta)` | PI on battery current, target 0 A | off |
| `recharge` | `CVL` (clamped to the CAN window) | `min(config max, CCL)` | off |

`delta` is `compensation_delta_v`, 0.5 V by default. It is not cosmetic: a
rectifier only sources current when its setpoint sits above the bus, and the
same offset bounds how much current the pack absorbs if the load suddenly
collapses (the bus cannot be pushed further than that ceiling). Lower it for a
gentler collapse, raise it if the rectifier refuses to deliver on a sagging bus.

The compensation loop is a PI in **velocity form** on the battery current:
`u += kp * Δerror + ki * error * dt`, clamped to `[0, max]` and slew limited.
Velocity form means no wind-up to manage and no bump when the mode changes.
Ramping up is slower than cutting down (`ramp_down_multiplier`), so a load
collapse kills the setpoint within one control period instead of feeding the
pack.

Tuning measured on the plant model (`tests/test_regulator.py`, 10 A load,
1 Hz): settles below 0.5 A of battery current in **8 s**, no overshoot past the
load, and a 20 A → 2 A load step recovers on the next period.

---

## Architecture

```
JK-BMS (CAN)        MPP Solar inverters
     │ dbus                │ dbus
     ▼                     ▼
 com.victronenergy.battery  com.victronenergy.inverter
     │  /Info/MaxChargeVoltage, /Info/MaxChargeCurrent, /Dc/0/*
     │
     ▼
 dbus-r4850G2-charger ── charger control loop (1 Hz) ──► CAN ──► R4850G2
     │  publishes com.victronenergy.charger.huawei-r4850g2.<serial>
     │  exposes /Mode, /Setpoint/*, /Diagnostics/*
```

Three layers, deliberately separated so the policy is testable off-target:

| File | Role | Testable off-target |
|---|---|---|
| `r4850_can.py` | pure codec: identifiers, register frames, telemetry decoding | yes, no I/O |
| `r4850_modes.py` | pure policy: mode → (voltage, current, standby) | yes, no clock, no D-Bus |
| `dbus-r4850G2-charger.py` | I/O: D-Bus service, SocketCAN, 1 Hz loop | dry-run mode |

---

## Protocol

Full register map and identifier layout: [docs/can-protocol.md](docs/can-protocol.md).
Sources: [patagonaa/huawei-r48xx](https://github.com/patagonaa/huawei-r48xx)
(verified on R4830S1, R4850G2, R4850G6) and
[craigpeacock/Huawei_R4850G2_CAN](https://github.com/craigpeacock/Huawei_R4850G2_CAN).

The four facts that shape the design:

1. **Telemetry is readable.** Command `0x40` returns output voltage, output
   current (fast and filtered), input voltage, temperatures, efficiency,
   alarms. beyondlogic's 2023 review states it is not; that was wrong.
2. **There is a standby register** (`0x0132`). "Off" does not mean "current
   limit zero and hope": the module can be put in standby and left there.
3. **Online mode times out after ~60 s** without a valid frame, and entering it
   resets volatile setpoints to the persistent defaults. The loop therefore
   transmits at 1 Hz, unconditionally, even in `off`.
4. **Persistent registers are never written** (`0x0101` default voltage,
   `0x0104` default current limit). The persistent default voltage cannot go
   below 48.0 V; writing it wrong can leave the unit unable to start.

---

## Safety invariants

Non-negotiable, enforced in code and asserted in tests:

1. The voltage setpoint never exceeds the BMS CVL. No CVL, no charging.
2. The current setpoint never exceeds the BMS CCL, nor the configured maximum.
3. Missing or stale battery data (default 10 s) drops the output to standby.
4. Voltage stays inside 41.0-58.6 V whatever the inputs say.
5. Every control period sends a frame, so the module never falls back to its
   persistent defaults behind our back.
6. Recharge refuses to run without a CVL: a guessed charge voltage on LiFePO4
   is the failure this driver exists to prevent.

---

## Tests

No dependency beyond the standard library, runnable anywhere:

```sh
python3 tests/test_r4850_can.py    # 21 tests, anchored on frames from real units
python3 tests/test_r4850_modes.py  # 15 tests, plant model + fail-safe paths
./run-tests.sh                     # both, and this is what runs on the GX
```

The codec tests use the exact frames published in the protocol documentation
(`01 75 00 00 00 00 CD B1` → 51.42 V, `01 03 00 00 00 00 01 25` → 10 A on a 35 A
module, and so on), so a regression in the encoding shows up as a byte
mismatch, not as a plausibility check.

---

## Status

| Piece | State |
|---|---|
| CAN codec + tests | done, verified against documented frames |
| Mode policy + tests | done, behaviour measured on the plant model |
| SocketCAN transport (AF_CAN, no python-can) | done |
| D-Bus `com.victronenergy.charger` service | done |
| Control loop, watchdog, standby on shutdown | done |
| Home Assistant over Modbus TCP (native register map) | done, no MQTT |
| Bench CLI (`r4850.py --info/--monitor/--standby`) | done |
| `install.sh` + runit service + can1 take-over | written, not run |
| On-target validation | code and tests run on the GX; no module wired yet |

Measured on the target (Venus OS v3.70, Python 3.12.12): compiles, 36 unit
tests pass, and `--self-test` runs the control loop closed on a simulated
plant (compensation converges, recharge saturates at CCL). What is *not*
validated: anything that needs the rectifier on the wire.

## D-Bus

``com.victronenergy.charger.huawei-r4850g2.<serial>`` — the AC charger type,
which is what the Venus GUI and VRM already know how to display.

| Path | Meaning |
|---|---|
| `/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Power` | rectifier **output**, from CAN telemetry (`0x0175`, `0x0181`, `0x0173`) |
| `/Dc/0/Temperature` | module output temperature (`0x017F`) |
| `/Ac/In/0/L1/V`, `/I`, `/P` | rectifier **input** (`0x0178`, `0x0172`, `0x0170`) |
| `/State` | 0 off, 2 fault (CAN lost), 3 bulk, 4 absorption |
| `/Mode` (w) | 1 on / 4 off — the plain switch GUI and VRM already draw |
| `/Settings/ChargeMode` (w) | 0 off, 1 compensation, 2 recharge — the real policy |
| `/Settings/MaxCurrent` (w) | current ceiling in A |
| `/Settings/CompensationDelta` (w) | voltage offset used in compensation |
| `/Link/ChargeVoltage`, `/Link/ChargeCurrent` | the BMS CVL/CCL actually used |
| `/Link/BatteryVoltage`, `/Link/BatteryCurrent` | the BMS values behind the loop |
| `/Diagnostics/Setpoint/*`, `/Diagnostics/Reason` | what the policy decided, in plain text |
| `/Diagnostics/Can/*` | frames, acks, refused writes, age of the last frame |
| `/Alarms/HighVoltage`, `/Alarms/HighTemperature` | derived from telemetry |

`/Mode` and `/Settings/ChargeMode` are deliberately separate: the standard
switch must stay a switch, the three-way policy needs its own path, and both
stay consistent (turning `/Mode` off stores the last active mode and turning it
back on restores it).

## Home Assistant — Modbus TCP, no MQTT

Two measurements decided this, both taken on the GX:

1. **The GX cannot reach a home broker.** From the GX, `192.168.10.100:1883`
   is unreachable — VLAN 20 routes outbound to the internet, not to the LAN
   (the UDM "Block Inter VLAN Internal" policy). A bridge from the GX to EMQX
   would need a firewall hole before a single message flows.
2. **Home Assistant already polls the GX on port 502.** `netstat` shows an
   established session `10.10.10.10 → 192.168.20.245:502`, and
   `com.victronenergy.charger` **is in the Modbus register map**
   (`/opt/victronenergy/dbus-modbustcp/attributes.csv`, registers 2307-2322).

So the charger shows up in the polling that already exists, with no new
infrastructure, no broker, no firewall change.

| Register | Path | R/W | Note |
|---|---|---|---|
| 2307 | `/Dc/0/Voltage` | R | uint16, /100 |
| 2308 | `/Dc/0/Current` | R | int16, /10 |
| 2309 | `/Dc/0/Temperature` | R | int16, /10 |
| 2310-2313 | `/Dc/1`, `/Dc/2` | R | unused here (single output) |
| 2314 | `/Ac/In/L1/I` | R | int16, /10 |
| 2315 | `/Ac/In/L1/P` | R | uint16, W |
| 2316 | `/Ac/In/CurrentLimit` | **W** | AC ceiling, turned into a DC cap |
| 2317 | `/Mode` | **W** | 0 off, 1 on, **2 recharge** (private) |
| 2318 | `/State` | R | 0 off, 2 fault, 3 bulk, 4 absorption |
| 2319 | `/ErrorCode` | R | |
| 2321/2322 | `/Alarms/LowVoltage`, `/HighVoltage` | R | |

Only **two** registers are writable for this service type, which is why the
three-way mode rides on `/Mode`: `0` off, `1` compensation, `2` recharge.

The driver publishes the real mode, including `2`, which the Victron register
map labels "Error". That is deliberate and measured: a writeable D-Bus path
fires its callback only when the value changes, so a `/Mode` published as a
0/1 view silently swallowed the command (writing `1` while it already read `1`
did nothing). Verified on 2026-11-07 with `/Mode = 2`: no error in the Remote
Console, none on VRM.

`/Ac/In/CurrentLimit` is implemented honestly instead of repurposed: the
R4850G2 has no AC current limit register (that is a G6 feature), so the value is
converted into a DC ceiling, `I_dc = I_ac × 230 V × 0.93 / V_battery`, and
applied as the regulator's maximum. That gives Home Assistant a real power
throttle without inventing a register.

The MQTT bridge that lived here up to commit `98007bd` was removed: it could not
reach a broker from VLAN 20 and duplicated a path that already exists. `git show
98007bd` if it is ever wanted behind a firewall hole.

## Installation

`can1` is taken over: `vecan-dbus.can1` is stopped and the interface is
reconfigured to **125 kbit/s** by [`r4850-can-up.sh`](r4850-can-up.sh), which
the runit service calls at every start. The CAN profile in Venus OS cannot
express 125 kbit/s (options are 250/500 kbit/s only), which is why this is not
done through Settings.

```sh
sh install.sh --dry-run   # prints the plan
sh install.sh             # installs and starts
```


`velib_python` submodule, fast-forwards the checkout, brings `can1` to
125 kbit/s, writes `/service/r4850-charger/run`, starts the service. State
(`/data/etc/dbus-r4850-state/settings.json`) and logs (`/var/log/dbus-r4850/`)
live outside the checkout. A firmware update restores the stock `/service`
tree; re-run the script afterwards.

## Still open

1. **Output contactor**: is the rectifier permanently wired to the bus? The
   output capacitors are large and will spark when connected to a live battery;
   standby disables the DC/DC stage but does not isolate the output.
2. **Venus GUI page for the three modes**: `/Mode` gives an on/off switch in the
   GUI and VRM for free. A page with the three-way selector needs a gui-v2
   plugin, as `dbus-mpp-p18` does; the plan is to copy that structure once the
   CAN side is proven.
3. **R4850G2 vs R4850G6**: fan duty cycle and AC input current limit are absent
   on the G2, whose fan follows the *input* temperature and lets the module run
   above 80 °C at full power. Fan mode `max` (`0x0134`) is the only lever, and
   the driver sets it above half the current ceiling.
