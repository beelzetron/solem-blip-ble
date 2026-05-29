# Solem BL-IP Bluetooth Protocol Documentation

## Overview

This document captures the discovered BLE protocol for the Solem BL-IP irrigation controller based on reverse engineering and live testing.

**Device:** Solem BL-IP (4- and 6-station models tested)  
**Controller Software Version:** 5.1.5  
**MAC Address:** C8:B9:61:D4:4D:C8 (example)

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

**Important:** Every command must be followed by `3b00` (commit) to execute.

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
| 4-7 | Station Data | Pattern `0x00aaaaaa` when active, `0x00000000` when idle |
| 9 | **Station Number** | Active station (1-6), 0 when idle |
| 13-14 | **Remaining Time** | Big-endian uint16, seconds remaining (only meaningful when watering) |
| 14-15 | Padding | Often `0x3c10` during watering; do not use for remaining time |
| 16-17 | Padding | Always `0x0000` |

### Status Byte (Byte 3)

| Value | Binary | Controller | Watering | Description |
|-------|--------|------------|----------|-------------|
| `0x40` | `01000000` | ON | IDLE | Controller ON, no active watering |
| `0x42` | `01000010` | ON | ACTIVE | Controller ON, actively watering |
| `0x02` | `00000010` | OFF | ACTIVE | Controller OFF, manual watering active |
| `0x00` | `00000000` | OFF | IDLE | Controller OFF, idle |
| `0x10` | `00010000` | - | - | Intermediate response (no state) |

**Bit Flags:**
- **Bit 6 (0x40)**: Controller permanent state (ON/OFF)
- **Bit 1 (0x02)**: Active watering flag

---

## Parsing Logic

### Controller State
```python
status_byte = notification[3]
is_controller_on = bool(status_byte & 0x40)
is_watering = bool(status_byte & 0x02)

controller_state = "On" if is_controller_on else "Off"
```

### Station Number
```python
station_num = notification[9]  # 1-6, or 0 if idle
```

### Remaining Time
```python
import struct
remaining_seconds = struct.unpack(">H", notification[13:15])[0]  # bytes 13-14, big-endian
```

Only parse when the watering flag is set (`status_byte & 0x02`). Ignore values outside a sane range (e.g. 1–14400 seconds).

**Wrong offset:** Reading bytes 14–16 (`notification[14:16]`) picks up padding and reports bogus values (e.g. `0x3c10` → 15376 s).

### HCI validation (MySOLEM app, firmware 5.1.5)

Command: `3105120100003c` (station 1, 60 s) + commit `3b00`

Notification (seq `0x02`):

```
3210024200aaaaaa00014f0c10003c100000
                              ^^^^
                           0x003c = 60 seconds at bytes 13-14
```

Cross-checked against Android Bluetooth HCI snoop logs and the official MySOLEM app (`BluetoothV5FrameManager` for firmware 5.x).

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
4. **Bytes 10–12:** Purpose not fully mapped; not required for status polling

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
            "is_watering": bool(status_byte & 0x02),
            "station_num": data[9] if 1 <= data[9] <= 6 else None,
            "remaining_seconds": struct.unpack(">H", data[13:15])[0],
        }
```

---

## Test Results Summary

### Commands and status
- ✅ Turn ON (`0x40`), station sprinkle (`0x42`), STOP, turn OFF (`0x00`)
- ✅ Stations 1–6 on 6-station BL-IP
- ✅ Remaining time at **bytes 13–14** (HCI + hardware + `scripts/validate_device.py`)

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

- **pcman75 Reverse Engineering Repo:** https://github.com/pcman75/solem-blip-reverse-engineering
- **Solem Product Page:** https://www.solem.fr/en/residential-watering/9-bl-ip.html

---

*Document created: 2026-05-28*  
*Last updated: 2026-05-29*  
*Status: Commands per pcman75; status notify protocol validated on BL-IP hardware and MySOLEM HCI captures (see `solem_blip_ble` implementation).*
