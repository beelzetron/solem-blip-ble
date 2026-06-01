"""Protocol constants for Solem BL-IP."""

WRITE_CHAR_UUID = "108b0002-eab5-bc09-d0ea-0b8f467ce8ee"
NOTIFY_CHAR_UUID = "108b0003-eab5-bc09-d0ea-0b8f467ce8ee"

COMMIT_COMMAND = bytes.fromhex("3b00")

# BL-IP supports up to 8 stations (4- and 6-station models validated; 8 is the spec max).
MAX_STATION_NUM = 8
DEFAULT_MAX_STATION_NUM = MAX_STATION_NUM

# BLE timing from observed controller behavior during hardware validation.
DEFAULT_BLUETOOTH_TIMEOUT = 30.0
OPERATION_TIMEOUT = 45.0
DISCONNECT_TIMEOUT = 3.0
SCAN_DURATION = 10.0
SCAN_PAUSE = 1.0
SCAN_MAX_ROUNDS = 3
RECONNECT_DELAY = 2.0
STATUS_NOTIFY_TIMEOUT = 30.0
REQUEST_RETRY_DELAY = 3.0
REQUEST_MAX_ATTEMPTS = 3
NOTIFY_SETTLE_DELAY = 0.5
NOTIFY_PARTIAL_RETRY_DELAY = 2.0
STATION_NAMES_IDLE_TIMEOUT = NOTIFY_PARTIAL_RETRY_DELAY

# Per pcman75/solem-blip-reverse-engineering (tested up to 15 days, 0x0f)
MAX_TURN_OFF_DAYS = 15

# Max manual sprinkle duration (minutes) × 60 — matches pack_sprinkle_station cap.
MAX_REMAINING_SECONDS = 240 * 60

# 9 V battery level thresholds (documented in docs/ble_protocol.md).
BATTERY_LEVELS_9V = (60, 65, 70, 75, 80)
BATTERY_VOLTAGE_ALERT_9V = 50
