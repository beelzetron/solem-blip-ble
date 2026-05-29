"""Protocol constants for Solem BL-IP."""

WRITE_CHAR_UUID = "108b0002-eab5-bc09-d0ea-0b8f467ce8ee"
NOTIFY_CHAR_UUID = "108b0003-eab5-bc09-d0ea-0b8f467ce8ee"

COMMIT_COMMAND = bytes.fromhex("3b00")

DEFAULT_BLUETOOTH_TIMEOUT = 15.0
DEFAULT_MAX_STATION_NUM = 6
NOTIFY_SETTLE_DELAY = 0.5

# Per pcman75/solem-blip-reverse-engineering (tested up to 15 days, 0x0f)
MAX_TURN_OFF_DAYS = 15

# 9 V battery thresholds from observed BLE behavior (documented 9V thresholds)
BATTERY_LEVELS_9V = (60, 65, 70, 75, 80)
BATTERY_VOLTAGE_ALERT_9V = 50
