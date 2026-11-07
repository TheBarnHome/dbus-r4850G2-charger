# Huawei R48xx CAN protocol — the subset this driver relies on

Compiled from [patagonaa/huawei-r48xx](https://github.com/patagonaa/huawei-r48xx)
(verified against R4830S1, R4850G2 and R4850G6) and
[craigpeacock/Huawei_R4850G2_CAN](https://github.com/craigpeacock/Huawei_R4850G2_CAN).
Every frame quoted here was captured from real hardware by those authors.

Physical layer: **125 kbit/s**, 29-bit extended identifiers, `CAN_H`/`CAN_L` +
`GND`. CAN is powered from the DC side, so the module answers even with no AC
input.

## Identifier layout

`000a aaaa abbb bbbb cccc cccc deee eefg`

| field | bits | meaning |
|---|---|---|
| a | 28-23 | protocol: `0x20` = PSU↔PSU, `0x21` = controller↔PSU |
| b | 22-16 | software address (0 = broadcast, 1 = first module) |
| c | 15-8 | command |
| d | 7 | source: 0 = from PSU, 1 = to PSU |
| e | 6-2 | group mask, always `0x1F` |
| f | 1 | hardware/software address flag, always 1 |
| g | 0 | more frames follow |

Worked example: `0x1081407E` → protocol `0x21`, address 1, command `0x40`,
from the PSU, last frame of a response.

## Commands

| cmd | direction | purpose |
|---|---|---|
| `0x40` | controller → PSU `108140FE` | data request |
| `0x40` | PSU → controller `1081407F` / `…7E` | data response, one register per frame |
| `0x50` | controller → PSU `108150FE` | info request |
| `0x50` | PSU → controller `1081507F` / `…7E` | info response |
| `0x80` | controller → PSU `108180FE` | register set |
| `0x80` | PSU → controller `1081807E` | set acknowledgement |
| `0x82` | controller → PSU `108182FE` | register get |
| `0x82` | PSU → controller `1081827E` | get response |

Multi-frame responses set bit 0 on every frame but the last. Each frame carries
the register id in bytes 0-1 and the value in bytes 2-7, big-endian.

## Registers written by this driver

| register | payload example | meaning |
|---|---|---|
| `0x0100` | `01 00 00 00 00 00 D6 00` | output voltage, `V * 1024` (53.5 V) |
| `0x0103` | `01 03 00 00 00 00 01 25` | current limit, `ratio * 1024`, 1024 = nominal |
| `0x0132` | `01 32 00 01 00 00 00 00` | standby: data byte 1 = `00` on, `01` standby |
| `0x0134` | `01 34 00 01 00 00 00 00` | fan mode: data byte 1 = `00` auto, `01` max |

Never written: `0x0101` (default voltage, persistent, floor 48.0 V) and
`0x0104` (default current limit, persistent). See AGENTS.md.

Not supported on the R4850G2, despite existing on G6 units: `0x0109` (AC current
limit), `0x0114` (fan duty cycle), `0x0187` (fan status). They answer with a
parameter error.

## Telemetry registers read by this driver

Command `0x40` response, value `/ 1024`:

| register | example | physical value |
|---|---|---|
| `0x010E` | `01 0E 00 00 00 00 2A 01` | operating hours |
| `0x0170` | `01 70 00 00 00 1C DC 31` | input power, 1847 W |
| `0x0171` | `01 71 00 00 00 00 C7 F5` | input frequency, 49.99 Hz |
| `0x0172` | `01 72 00 00 00 00 20 91` | input current, 8.14 A |
| `0x0173` | `01 73 00 00 00 1C 20 9A` | output power, 1800 W |
| `0x0174` | `01 74 00 00 00 00 03 E6` | efficiency, 0.975 |
| `0x0175` | `01 75 00 00 00 00 CD B1` | output voltage, 51.42 V |
| `0x0176` | `01 76 00 00 00 00 04 00` | max output current, ratio of nominal |
| `0x0178` | `01 78 00 00 00 03 8B 80` | input voltage, 226.9 V |
| `0x017F` | `01 7F 00 00 00 00 84 00` | output temperature, 33 °C |
| `0x0180` | `01 80 00 00 00 00 6C 00` | input temperature, 27 °C |
| `0x0181` | `01 81 00 00 00 00 8C 11` | output current, fast/unfiltered, 35.02 A |
| `0x0182` | `01 82 00 00 00 00 8C 07` | output current, filtered, 35.01 A |
| `0x0183` | `01 83 00 00 10 00 00 00` | alarm/status bits |

Temperatures are signed 48-bit two's complement. `0x0181` reads closer to a
series bench meter than `0x0182`; the driver publishes the fast one as
`/Dc/0/Current` and keeps the filtered one as a diagnostic.

## Info registers read by this driver

Command `0x50` response:

| register | example | meaning |
|---|---|---|
| `0x0001` | `00 01 00 00 40 46 12 67` | nominal current: `((raw >> 16) & 0x3FF) / 2` = 35 A |
| `0x0002` | `00 02 24 68 23 8C 3C 2F` | serial number, 6 bytes |
| `0x0003` | `00 03 32 31 30 32 33 31` | barcode part 1, ASCII |
| `0x0004` | `00 04 30 48 42 4A 44 30` | barcode part 2, ASCII |
| `0x0005` | `00 05 01 00 02 01 02 01` | HW / DC-DC SW / PFC SW versions |
| `0x0006` | `00 06 01 01 00 00 00 00` | slot ids (top pin, bottom pin) |

The nominal current is not cosmetic: the current limit register is a ratio of
it, so a driver that writes amps without reading `0x0001` first is off by the
ratio between modules (35 A on R4830S1, ~52 A on R4850G6).

## Set acknowledgement

| direction | frame | meaning |
|---|---|---|
| success | `01 00 00 00 00 00 D6 00` | byte 0 = `0x01`, echoed request |
| failure | `21 00 …` | byte 0 = `0x21`, bit 5 set, status `2` = parameter error |

The status is `payload[0] >> 4`; the register id keeps the low nibble of that
byte.

## Online / offline behaviour

- **Offline**: no valid CAN traffic. The module uses its persistent default
  voltage and current limit, front panel steady.
- **Online**: a valid frame addressed to it was seen within the last ~60 s. The
  volatile setpoints apply. Front panel LED flashes yellow if the timeout
  approaches.
- Entering online mode resets the volatile setpoints to the persistent defaults.
  A controller must therefore treat the first frames after a gap as an
  initialisation, not as an update.

## Accepted ranges

| parameter | range |
|---|---|
| output voltage (online) | 41.0 V (`A4 00`) to 58.6 V (`EA 67`) |
| default output voltage (persistent) | 48.0 V (`C0 00`) to 58.4 V (`E9 9A`) |
| current limit | 0 to 1250/1024 of nominal (122 %), 1024 = nominal |

The module rejects out-of-range writes with a parameter error rather than
clamping, which is why the encoder clamps first.
