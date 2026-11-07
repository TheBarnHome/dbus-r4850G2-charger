# AGENTS.md — contract for dbus-r4850G2-charger

Read this before touching the driver. Everything here was paid for once.

## Scope

Drive one or more Huawei R48xx rectifiers from Venus OS over SocketCAN, as an
AC charger on a Victron DC bus, in three modes: `off`, `compensation`,
`recharge`. The BMS is the authority on voltage and current; the driver is an
executor, never an authority.

## Layer boundaries — do not mix them

| Layer | Rule |
|---|---|
| `r4850_can.py` | pure functions, no I/O, no globals. If it needs the bus, it is the wrong layer. |
| `r4850_modes.py` | pure policy, takes an explicit `now` in seconds, no D-Bus, no CAN, no `time.time()`. |
| `dbus-r4850G2-charger.py` | the only file allowed to touch D-Bus, SocketCAN, files, or the clock. |

Rationale: the two pure layers are the ones that can be tested on a laptop and
on the CI of a homelab, and they hold every decision that can hurt the battery.

## Invariants (assert them, do not comment them)

1. Voltage setpoint ≤ BMS CVL, always, in every mode, including transients.
2. Current setpoint ≤ BMS CCL and ≤ `RegulatorConfig.max_current_a`.
3. No battery CVL → `recharge` is refused (standby), never a guessed voltage.
4. Missing or older-than-`battery_stale_s` battery data → standby, current 0.
5. Voltage inside `[VOLTAGE_MIN, VOLTAGE_MAX]` = `[41.0, 58.6]` V.
6. A CAN frame leaves the controller every control period, in every mode.
7. Persistent registers `0x0101` and `0x0104` are never written. See traps.

## Traps

### Register ids: `0x0100` writes voltage, `0x0175` reads it

Two different registers, two different directions, and the natural names
collide. The first version of this codec named both `REG_OUTPUT_VOLTAGE`; the
second definition silently won and every setpoint went out on the telemetry
register. Setpoint constants are `REG_*`, telemetry constants are `DATA_*`.
Keep it that way.

### Not every register is a u48

`0x0132` (standby) and `0x0134` (fan mode) carry a flag in the **second data
byte**: `01 32 00 01 00 00 00 00` is standby, `01 34 00 01 00 00 00 00` is fan
max. Encoding them through `encode_u48()` writes `... 00 00 01` instead, which
the module answers with a parameter error — or worse, accepts as zero. Use
`encode_register_bytes()`.

### Acknowledgement status is the **high** nibble

A successful `0x80` acknowledgement echoes the request byte for byte and starts
with `0x01`. A rejected one raises bit 5, so it starts with `0x21`. The status
is therefore `payload[0] >> 4`, and the register id keeps the low nibble of that
first byte. Reading `payload[0] & 0x0F` reports "error 1" on every success.

### Online mode forgets everything after ~60 s

The module keeps receiving nothing, returns to the *persistent* default voltage
and current limit, and flashes the yellow LED. Consequences: transmit at 1 Hz
even in `off`, and never infer the module state from a cached setpoint — read
the telemetry back.

### `/ErrorCode` belongs to the device, not to the driver

The Victron charger error list is specific: `1` is "Battery temperature too
high", `2` "Battery voltage too high", and so on. The first version published
`1` to signal "no module on the CAN", and the GUI and VRM reported a battery
over-temperature on a bus with nothing connected. An absent device is
`/Connected = 0` and `/State = 0`, with the reason in `/Diagnostics/Reason`.

### A writeable D-Bus path only fires on change

`onchangecallback` is not called when a client writes the value the path already
holds. A two-state register can therefore never carry a three-state command: the
second write is dropped in silence, with no error anywhere. Either publish the
full domain of the value (this driver publishes `/Mode` as 0/1/2) or have the
caller force a transition through a different value first.

### The module may not be there when the service starts

The info request (command `0x50`) is what yields the serial and the nominal
current, and the service cannot register without the serial. Powering the
rectifier up after the driver, or swapping a module, is the normal case: the
request has to be re-sent every few seconds until the module answers, not fired
once at start-up. `INFO_RETRY_S` exists for that reason.

### The setpoint cache lies after a CAN outage

`apply_to_module()` only writes values that changed, which is what keeps the
bus quiet. But the module falls back to its persistent defaults after ~60 s of
silence, so after an outage the cache says "already applied" while the module
holds something else — and the driver would never rewrite it. Every setpoint is
therefore rewritten unconditionally after `REWRITE_EVERY_S` (30 s), and the
cache is dropped on recovery.

### The persistent defaults are a trap, not a convenience

`0x0100`/`0x0103` are volatile and die with the online session. `0x0101`/`0x0104`
survive reboots. The persistent default voltage cannot be programmed below
48.0 V, and the module falls back to it whenever CAN goes quiet: write it wrong
and the unit can refuse to start or, worse, start into a pack at a voltage
nobody asked for. The driver writes the volatile registers only, and leaves the
fallback to the module's factory values.

### Fan control on the G2

The R4850G2 has no fan duty cycle register and no AC input current limit
(both return a parameter error). Its fan follows the *input* temperature, so at
full load the silicon happily runs above 80 °C. The only lever is fan mode
`0x0134 = max`, and it is persistent. Set it when the current setpoint is high,
and do not expect the duty-cycle register to exist.

### Capacitors and a live bus

The output capacitors are large. Connecting an unpowered rectifier to a live
battery bus sparks and can weld a connector. Standby disables the DC/DC stage
but does not isolate the output. Any installation needs a precharge path or a
contactor, and that decision is not the driver's.

## Testing rules

- Every codec behaviour is anchored on a frame published in the protocol
  documentation, not on a round trip of our own encoder. A round trip proves
  self-consistency, not correctness.
- Regulator tests use the trivial plant `i_battery = i_rectifier - i_load`.
  It is enough to prove convergence, the voltage ceiling, the fail-safe paths
  and the asymmetry between ramping up and cutting down. Do not replace it with
  a mock that only asserts calls.
- A test asserting a physical impossibility is a bug in the test. A load
  collapse at 1 Hz is seen one period late, by construction.

## Conventions

- No external dependency at runtime: Venus OS provides Python 3 and
  `dbus-python`; the codec and the regulator use the standard library only.
- Install under `/data/etc/dbus-r4850G2-charger`, persistent state outside the
  Git checkout, `/data` is the only partition that survives a firmware update.
- Service names carry the module serial: `com.victronenergy.charger.huawei-r4850g2.<serial>`.
- Nothing is written to the module before its info response has been read: the
  nominal current from info register `0x0001` defines the scale of the current
  limit.
