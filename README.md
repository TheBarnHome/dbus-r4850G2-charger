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
python3 tests/test_can.py        # 21 tests, anchored on frames captured from real units
python3 tests/test_regulator.py  # 15 tests, plant model + fail-safe paths
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
| D-Bus service | not written |
| SocketCAN transport + watchdog | not written |
| `install.sh` / runit service / update path | not written |
| On-target validation | not started (no hardware attached yet) |

### Open questions before the I/O layer

1. **CAN interface** — measured on the target, 2026-11-07: both CAN ports are
   `mcp251x` channels of the same SPI HAT. `can0` runs at **500 kbit/s** for the
   JK-BMS (`can-bus-bms.can0`, 33 990 frames received, 0 errors), `can1` runs at
   **250 kbit/s** for VE.Can and has moved **zero frames** in either direction.
   The R4850G2 is fixed at **125 kbit/s**, so it cannot share `can0`: one bus,
   one bitrate, and the BMS link carries CVL/CCL — the last link to expose to a
   3 kW rectifier. Two ways out:
   - **dedicated USB adapter** (candleLight / gs_usb, module present in the
     Venus OS kernel) → `can2` at 125 kbit/s, zero coupling, ~20 EUR;
   - **reuse `can1`** at 125 kbit/s if VE.Can is definitively unused, which
     costs the VE.Can capability and needs the driver to alarm on bus-off (the
     profile lives in `/data/conf/settings.xml`, a firmware update can revert it).
2. **Mode selection**: D-Bus `/Mode` written by Home Assistant or Node-RED,
   a switch in the Venus GUI, or an external policy (energy-planner)? The
   driver exposes the path either way; who decides changes the interface.
3. **Output contactor**: is the rectifier permanently wired to the bus? The
   output capacitors are large and will spark on connection to a live battery;
   standby keeps the DC/DC off but does not isolate it.
4. **R4850G2 vs R4850G6**: fan duty cycle and AC input current limit are absent
   on the G2, and the fan is driven by the *input* temperature, which lets the
   module run above 80 °C at full power. Fan mode `max` (register `0x0134`) is
   the only lever on the G2, and this driver sets it whenever the current
   setpoint is above 50 %.
