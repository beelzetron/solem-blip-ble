# Solem BL-IP Bluetooth Protocol Documentation

## Overview

This document captures the discovered BLE protocol for the Solem BL-IP irrigation controller based on protocol analysis and live hardware testing.

**Device:** Solem BL-IP (4- and 6-station models tested)  
**Controller Software Version:** 5.1.5  
**MAC Address:** device-specific (pass on the CLI, e.g. `AA:BB:CC:DD:EE:FF`)

---

## GATT Characteristics

| UUID | Purpose | Properties |
|------|---------|------------|
| `108b0002-eab5-bc09-d0ea-0b8f467ce8ee` | **Write** - Send commands to device | Write, Write Without Response |
| `108b0003-eab5-bc09-d0ea-0b8f467ce8ee` | **Notify** - Device sends status updates | Notify |

---

## Command Protocol

### Command Format

All commands follow this structure:
```
3105 XX YY ZZ WWWW
```

| Byte(s) | Description |
|---------|-------------|
| `3105` | Fixed header |
| `XX` | Command type |
| `YY` | Parameter 1 (station, days, etc.) |
| `ZZ` | Parameter 2 (usually 00) |
| `WWWW` | Duration in seconds (big-endian) |

**Important:** Irrigation control commands must be followed by `3b00` (commit) to execute. Set-time (`0306`) and read requests do not use commit.

### Set Device Time

Write-only command to sync local date/time to the device RTC:

```
03 06 00 YY MM DD hh mm ss
```

| Byte | Meaning |
|------|---------|
| 0 | `0x03` — set time |
| 1 | `0x06` — subtype |
| 2 | `0x00` |
| 3 | year minus 1900 |
| 4 | month (`1-12`) |
| 5 | day of month |
| 6 | hour |
| 7 | minute |
| 8 | second |

There is no matching read-time command on BL-IP V5.

### Known Commands

| Command | Code | Description |
|---------|------|-------------|
| Turn ON | `3105a000000000` + `3b00` | Enable controller (scheduled programs run) |
| Turn OFF (permanent) | `3105c000000000` + `3b00` | Disable controller permanently |
| Turn OFF (N days) | `3105c0000N0000` + `3b00` | Disable for N days (1-15) |
| All stations | `3105110000SSSS` + `3b00` | All stations for SSSS seconds |
| Station X | `3105120X00SSSS` + `3b00` | Station X for SSSS seconds |
| Program X | `310514000X0000` + `3b00` | Run program X |
| STOP | `31051500ff0000` + `3b00` | Stop active watering |

### Read Station Name

Station names are stored on V5 controllers as output names. Reading a name uses
a separate request and does not require the `3b00` commit:

```
35 00
```

The controller replies with two notifications per output:

```
36 12 SS NN [up to 16 UTF-8 bytes]
```

On hardware the response type is `0x36` (response to the `0x35` read). Byte 1 is
always `0x12`. `SS` is a sequence counter whose least significant bit identifies
the fragment (`SS & 1 == 1` first, `SS & 1 == 0` last). `NN` is the zero-based
output index (`00` for station 1). Concatenate the payloads at bytes 4-19 in
fragment order, then stop each fragment at its first NUL byte. Names are at most
32 bytes.

### Read Firmware Version

Firmware metadata is returned by the V5 identification command:

```
0f 00
```

The controller sends a wrapped identification notification:

```
10 0f 01 [MAC 6 bytes] [HW info] [major] [minor] [patch] ...
```

The subtype at byte 2 is `0x01`. Firmware major, minor, and patch are at bytes
12-14 regardless of the leading `0x10` wrapper byte. A second notification may
follow with the controller display name.

---

## V5 Irrigation Schedule / Config Frames

The BL-IP V5 controller has a second BLE protocol family for persisted schedule
configuration. This is distinct from the simple manual-control `3105 ...` frames
documented above.

At a high level, the persisted on-device schedule is:

- 3 irrigation programs: `A`, `B`, `C`
- up to 8 start times per program
- up to 12 station durations per program
- one cycle mode per program
- one water budget per program
- one inter-station delay per program
- controller-level `programmationType`
- controller-level `calendarDayOffDay` / `calendarDayOffMonth`

### Read Requests

These V5 read requests retrieve controller config and output metadata:

| Request | Meaning |
|---------|---------|
| `39 00` | Read controller irrigation characteristics |
| `35 00` | Read station/output names |

`39 00` reads controller-level irrigation characteristics and persisted
irrigation program blocks. `35 00` reads station/output names as documented in
the command protocol section above.

### Controller Characteristics Write (`0x3f` / `0x10`)

The V5 writer emits two controller-level frames before or alongside per-program
writes.

#### Frame 1: `0x3f 0x11 0x00 0x02 ...`

19 bytes total:

| Byte | Meaning |
|------|---------|
| 0 | `0x3f` |
| 1 | `0x11` |
| 2 | `0x00` |
| 3 | `0x02` |
| 4 | `programmationType` (`0 = EU`, `1 = US`) |
| 5-18 | Monthly water budgets 0-6, 7 values as big-endian uint16 |

If monthly water budgets are disabled, each monthly slot is written as
`0x0064` (`100`).

#### Frame 2: `0x3f 0x10 0x01 ...`

18 bytes total:

| Byte | Meaning |
|------|---------|
| 0 | `0x3f` |
| 1 | `0x10` |
| 2 | `0x01` |
| 3-12 | Monthly water budgets 7-11, 5 values as big-endian uint16 |
| 13 | Station flags for outputs 1-4 |
| 14 | Station flags for outputs 5-8 |
| 15 | Station flags for outputs 9-12 |
| 16 | `calendarDayOffDay` |
| 17 | `calendarDayOffMonth` |

Each station flag byte packs 4 outputs in 2 bits each:

- bit `7 - 2*n`: use master valve
- bit `6 - 2*n`: use sensor

for station slot `n` inside that byte.

On readback, `calendarDayOffDay` and `calendarDayOffMonth` are treated as
unset when the raw byte is `0`.

### Per-Program Write Frames (`0x2f`, `0x37`)

For irrigation program index `i` (`0`, `1`, `2`), the writer uses a tag byte:

```text
tag = 0x10 | i
```

Examples:

- program A (`i = 0`) -> tag `0x10`
- program B (`i = 1`) -> tag `0x11`
- program C (`i = 2`) -> tag `0x12`

#### Program Name Frames (`0x2f 0x12`)

Two 20-byte frames:

| Byte | Meaning |
|------|---------|
| 0 | `0x2f` |
| 1 | `0x12` |
| 2 | fragment index: `0x00` or `0x01` |
| 3 | program tag (`0x10 | i`) |
| 4-19 | UTF-8 name payload |

Program names are written as at most 31 bytes of UTF-8 data:

- fragment `0x00`: first 16 bytes
- fragment `0x01`: remaining 15 bytes plus trailing zero padding

#### Program Header Frame (`0x37 0x0e 0x00`)

16 bytes total:

| Byte | Meaning |
|------|---------|
| 0 | `0x37` |
| 1 | `0x0e` |
| 2 | `0x00` |
| 3 | program tag (`0x10 | i`) |
| 4-5 | `timeInterStation`, big-endian uint16 |
| 6-7 | `waterBudget`, big-endian uint16 |
| 8 | `cycle` |
| 9 | `weekDays` |
| 10 | `periodLength` |
| 11 | `synchroDay` |
| 12 | current day-of-month |
| 13 | current month (`1-12`) |
| 14-15 | current year, big-endian uint16 |

Notes:

- `cycle` values are defined as:
  - `0`: custom weekdays
  - `1`: even days
  - `2`: odd days
  - `3`: odd days except 31
  - `4`: periodic every `periodLength` days
- In V5 irrigation program frames, `weekDays` uses a weekday bitmask
  (`Mon` bit `0` through `Sun` bit `6`). Older V3 frames use a different
  on-wire encoding (see Weekday Mask Conversion Reference below).

#### Start Times Frame (`0x37 0x12 0x01`)

20 bytes total:

| Byte | Meaning |
|------|---------|
| 0 | `0x37` |
| 1 | `0x12` |
| 2 | `0x01` |
| 3 | program tag (`0x10 | i`) |
| 4-19 | 8 start times, each big-endian uint16 minutes since midnight |

Encoding rules:

- valid range: `0..1440`
- invalid or disabled start time is written as `1440`
- on readback, any decoded value `>= 1440` is treated as `-1` / disabled

#### Station Durations Frames

Station durations are stored as 3-byte big-endian integers, in seconds.

Frame layout:

| Frame | Bytes | Stations |
|-------|-------|----------|
| `0x37 0x11 0x02` | 19 bytes | stations 1-5 |
| `0x37 0x11 0x03` | 19 bytes | stations 6-10 |
| `0x37 0x08 0x04` | 10 bytes | stations 11-12 |

Shared structure:

| Byte | Meaning |
|------|---------|
| 0 | `0x37` |
| 1 | frame subtype (`0x11` or `0x08`) |
| 2 | chunk index (`0x02`, `0x03`, `0x04`) |
| 3 | program tag (`0x10 | i`) |
| 4.. | packed durations, 3 bytes per station |

Packing:

- chunk `0x02`: station indexes `0..4`
- chunk `0x03`: station indexes `5..9`
- chunk `0x04`: station indexes `10..11`

Write behavior:

- zero duration is written as `00 00 00`
- positive duration is written as:
  - high byte: `(seconds >> 16) & 0xff`
  - mid byte: `(seconds >> 8) & 0xff`
  - low byte: `seconds & 0xff`

### V5 Readback Frame Map

The V5 reader decodes schedule/config notifications with this structure:

- Hardware responds with type `0x3a` (response offset for the `0x39` read).
  Notifications may also arrive with a leading `0x10` wrapper byte.
- `byte 1`: frame subtype (`0x12` name/start-times, `0x0e` header, `0x11`
  durations, `0x08` final duration chunk). Matches the write-frame subtypes.
- `byte 2`: descending sequence counter. On BL-IP hardware this counts down
  across the full config dump (not a fixed `6..0` range per program). Group
  notifications by `byte 3` program tag, take the highest `byte 2` seen for
  that program as `first_fragment_id`, then compute
  `logical_chunk = first_fragment_id - byte 2`.
- `byte 3`: program tag — high nibble `0x1` = irrigation class, low nibble =
  program index (`0x10` = program A, `0x11` = B, `0x12` = C).
- Payload fields start at `byte 4`. Frames are variable length (for example
  the header and final duration chunk are shorter than the 20-byte name
  frames).
- `byte 3 low nibble`: program index
- `byte 3 high nibble`: program class
  - `0x1`: irrigation
  - `0x2`: lighting
  - `0x3`: misting
- `byte 2`: descending fragment id inside the current program block

The reader captures the first fragment id seen for a program, then computes:

```text
logical_chunk = first_fragment_id - current_fragment_id
```

For irrigation programs (`high nibble == 0x1`), the logical chunks are:

| Logical Chunk | Meaning | Parsed Fields |
|---------------|---------|---------------|
| `0` | program name, part 1 | bytes `4-19`, stop at first NUL |
| `1` | program name, part 2 | bytes `4-19`, stop at first NUL |
| `2` | program header | `timeInterStation`, `waterBudget`, `cycle`, `weekDays`, `periodLength` |
| `3` | start times | 8 x big-endian uint16 from bytes `4-19` |
| `4` | station durations 1-5 | 5 x 3-byte int from bytes `4-18` |
| `5` | station durations 6-10 | 5 x 3-byte int from bytes `4-18` |
| `6` | station durations 11-12 | 2 x 3-byte int from bytes `4-9` |

Readback details:

- chunk `2` reads:
  - bytes `4-5`: `timeInterStation`
  - bytes `6-7`: `waterBudget`
  - byte `8`: `cycle`
  - byte `9`: `weekDays`
  - byte `10`: `periodLength`
  - byte `11`: `synchroDay` (periodic phase anchor for cycle 4)
- chunk `3` reads all 8 start times from bytes `4-19`
- chunks `4-6` decode durations as 3-byte big-endian integers
- a start time `>= 1440` becomes disabled (`-1`)

Bytes `12-15` on the header chunk store the **period start date** when the program
is written (day-of-month, month `1-12`, year big-endian). On readback they reflect
the configured start date for cycle 4 (“every N days” from that anchor).

### Station Name / Program Mapping Compatibility Path

An older compact configuration format also emits:

- three program start-time blocks
- one station mapping block per output

That compact path uses one station frame with:

- output name
- selected program index for that station
- station duration in minutes

This is useful as a cross-check for the logical data model, but the richer V5
program writer frames documented above are the authoritative schedule/config
path for BL-IP V5 implementation work.

### Weekday Mask Conversion Reference

V3 and compatibility frames encode weekdays with a different on-wire bitmask.
The V5 weekday mask (`Mon=bit0 ... Sun=bit6`) converts to this form with:

- Monday -> `0x80`
- Tuesday -> `0x40`
- Wednesday -> `0x20`
- Thursday -> `0x10`
- Friday -> `0x08`
- Saturday -> `0x04`
- Sunday -> `0x02`

The older V3 and compatibility paths use this conversion. V5 irrigation program
frames write the weekday mask directly in the program header frame.

---

## Notification Protocol

### Notification Format (18 bytes)

```
Byte:  00  01  02  03  04  05  06  07  08  09  10  11  12  13  14  15  16  17
      ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
      │ RT │ 10 │SEQ │STAT│    0xaa    │    ??    │    ??    │TIME│ pad│    0000    │
      └────┴────┴────┴────┴────────────┴──────────┴──────────┴────┴────┴────────────┘
                                                      ^^^^^^^^
                                                   bytes 13-14 (BE uint16, seconds)
```

### Field Definitions

| Byte(s) | Field | Description |
|---------|-------|-------------|
| 0 | Response Type | `0x32` (response to 0x31xx), `0x3c` (response to 0x3bxx) |
| 1 | Constant | Always `0x10` |
| 2 | Sequence | `0x02` = Full data, `0x01` = Intermediate, `0x00` = Final |
| 3 | **Status** | Controller state flags (see below) |
| 4 | **Rain delay / off days** | `byte & 0x3f`: `1..15` = temporary off-days while controller is OFF |
| 5-7 | Station Data | Pattern `0xaaaaaa` when active, `0x000000` when idle |
| 8 | **Active program** | 1-based index while a program runs: `1` = A, `2` = B, `3` = C, `0` = none (station-only manual) |
| 9 | **Station Number** | Active station (1-6), 0 when idle |
| 10 | **Battery Voltage** | Raw 9 V reading (status notification, byte 10) |
| 13-14 | **Remaining Time** | Big-endian uint16, seconds remaining (only meaningful when watering) |
| 14-15 | Padding | Often `0x3c10` during watering; do not use for remaining time |
| 16-17 | Padding | Always `0x0000` |

### Status Byte (Byte 3)

| Value | Binary | Controller | Watering | Description |
|-------|--------|------------|----------|-------------|
| `0x40` | `01000000` | ON | IDLE | Controller ON, no active watering |
| `0x42` | `01000010` | ON | ACTIVE | Controller ON, manual/station watering |
| `0x44` | `01000100` | ON | ACTIVE | Controller ON, program run (byte 8 = program 1–3) |
| `0x02` | `00000010` | OFF | ACTIVE | Controller OFF, manual watering active |
| `0x00` | `00000000` | OFF | IDLE | Controller OFF, idle |
| `0x10` | `00010000` | - | - | Intermediate response (no state) |

**Bit Flags:**
- **Bit 6 (0x40)**: Controller permanent state (ON/OFF)
- **Bit 1 (0x02)**: Manual / station watering active
- **Bit 2 (0x04)**: Program run active (often `0x44` with controller ON)

---

## Parsing Logic

### Controller State
```python
status_byte = notification[3]
is_controller_on = bool(status_byte & 0x40)
is_watering = bool(status_byte & 0x06)  # 0x02 manual, 0x04 program

controller_state = "On" if is_controller_on else "Off"
```

### Controller Off Days
```python
off_days = notification[4] & 0x3F
if is_controller_on:
    controller_off_mode = "on"
    controller_off_days_remaining = 0
elif 1 <= off_days <= 15:
    controller_off_mode = "temporary"
    controller_off_days_remaining = off_days
else:
    controller_off_mode = "permanent"
    controller_off_days_remaining = 0
```

### Station Number
```python
station_num = notification[9]  # 1-6, or 0 if idle
```

### Remaining Time
```python
import struct
# Prefer 3-byte big-endian duration at bytes 12-14 (mask first byte with 0x0F).
# Station 2 may use bytes 15-17. Stations 3+ may also publish remaining in seq=0x01
# notifications at offset (station - 3) * 3 + 3.
remaining_seconds = parse_remaining_seconds(notification, station_num)
```

Only parse when a watering flag is set (`status_byte & 0x06`). Ignore values outside a sane range (e.g. 1–14400 seconds).

### Battery (9 V)

BL-IP reports raw battery voltage at **byte 10** of seq=0x02 status frames.

```python
battery_voltage = notification[10]  # 0 = not reported
```

Map to icon level 0–5 using documented 9 V thresholds `{60, 65, 70, 75, 80}`. Alert below **50**.

Example from HCI capture while watering (`3210024200aaaaaa00014f0c10003c100000`):

- byte 10 = `0x4f` (79) → level **4** (full bar at ≥80)
- bytes 13–14 = `0x003c` (60 s remaining)

**Wrong offset:** Reading bytes 14–16 (`notification[14:16]`) picks up padding and reports bogus values (e.g. `0x3c10` → 15376 s).

### HCI validation (firmware 5.1.5)

Command: `3105120100003c` (station 1, 60 s) + commit `3b00`

Notification (seq `0x02`):

```
3210024200aaaaaa00014f0c10003c100000
                              ^^^^
                           0x003c = 60 seconds at bytes 13-14
```

Validated via Bluetooth HCI snoop capture on BL-IP firmware 5.1.5.

---

## Protocol Behavior

### Command-Response Pattern

1. Send command (e.g., `3105a000000000`)
2. Send commit (`3b00`)
3. Device responds with 3 notifications:
   - Sequence `0x02`: Full response with data
   - Sequence `0x01`: Intermediate acknowledgment
   - Sequence `0x00`: Final/empty response

### Spontaneous Notifications

**Unknown:** Testing needed to determine if device sends periodic status updates without commands.

### Time Decrement

The remaining time (bytes 13-14, big-endian) decrements as watering progresses. Observed values:
- Initial: 180s (after Station 1 for 3 min command)
- After 45s: 142s (expected ~135s, slight discrepancy)

---

## Known Issues / Open Questions

1. **Spontaneous Notifications:** Unknown if device sends periodic status updates without a prior command
2. **Error Codes:** Unknown status byte values for error conditions
3. **Battery Level:** Not observed in notifications
4. **Bytes 11–12:** Purpose not fully mapped; not required for status polling

---

## Implementation Notes

### Recommendation for Home Assistant Integration

1. **Always send the commit (`3b00`)** after every command
2. **Parse only sequence `0x02`** notifications for actual data
3. **Use sequence `0x00`** as acknowledgment that command completed
4. **Graceful degradation:** If notifications fail, keep last known state
5. **Polling:** May need to poll periodically if no spontaneous notifications

### get_status() Implementation Strategy

```python
async def get_status(self) -> dict:
    """Get controller status from BLE notifications."""
    
    def parse_notification(data: bytes) -> dict:
        if len(data) < 18 or data[2] != 0x02:  # Only parse full data
            return None
        
        status_byte = data[3]
        
        return {
            "controller_state": "On" if status_byte & 0x40 else "Off",
            "controller_off_mode": "on" if status_byte & 0x40 else "temporary/permanent",
            "controller_off_days_remaining": data[4] & 0x3f,
            "is_watering": bool(status_byte & 0x06),
            "station_num": data[9] if 1 <= data[9] <= 6 else None,
            "remaining_seconds": struct.unpack(">H", data[13:15])[0],
            "battery_voltage": data[10] or None,
            "battery_level": battery_level_9v(data[10]) if data[10] else None,
        }
```

---

## Test Results Summary

### Commands and status
- ✅ Turn ON (`0x40`), station sprinkle (`0x42`), STOP, turn OFF (`0x00`)
- ✅ Stations 1–6 on 6-station BL-IP
- ✅ Remaining time at **bytes 13–14** (HCI + hardware + `scripts/validate_device.py`)
- ✅ Battery voltage at **byte 10** (HCI capture `0x4f` → level 4)
- ✅ Temporary off-days at **byte 4 & 0x3f**

### Remaining time
- ✅ 60 s sprinkle reads `0x003c` at bytes 13–14 (see HCI validation above)
- ✅ Time decrements during watering
- ❌ Bytes 14–16 must not be used (padding reads as ~14000+ seconds)

### Open
- ⏳ Spontaneous notifications without polling

---

## Quick Reference

| Status byte | Controller | Watering |
|-------------|------------|----------|
| `0x40` | ON | idle |
| `0x42` | ON | active |
| `0x02` | OFF | active (manual) |
| `0x00` | OFF | idle |

Parse only notifications with `data[2] == 0x02`. Ignore `0x10` in byte 3 (intermediate).

| Action | Command + commit |
|--------|------------------|
| Turn ON | `3105a000000000` + `3b00` |
| Turn OFF | `3105c000000000` + `3b00` |
| Station X for Y seconds | `3105120X00YYYY` + `3b00` |
| STOP | `31051500ff0000` + `3b00` |

---

## References

- **pcman75 Protocol Reference:** https://github.com/pcman75/solem-blip-reverse-engineering
- **Solem Product Page:** https://www.solem.fr/en/residential-watering/9-bl-ip.html

---

*Document created: 2026-05-28*  
*Last updated: 2026-05-29*  
*Status: Commands validated against pcman75 reference; status notify protocol validated on BL-IP hardware and HCI capture (see `solem_blip_ble` implementation).*
