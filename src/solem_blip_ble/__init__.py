"""BLE client for Solem BL-IP irrigation controllers."""

from .client import SolemClient
from .const import DEFAULT_MAX_STATION_NUM, MAX_STATION_NUM
from .exceptions import SolemConnectionError
from .protocol import (
    FirmwareVersion,
    is_command_notification,
    pack_commit,
    pack_get_firmware_version,
    pack_run_program,
    pack_sprinkle_all_stations,
    pack_sprinkle_station,
    pack_stop_manual_sprinkle,
    pack_turn_off_permanent,
    pack_turn_off_x_days,
    pack_turn_on,
    parse_battery_9v,
    parse_firmware_version_response,
    parse_status_notification,
)

# Back-compat alias used by Home Assistant integrations
APIConnectionError = SolemConnectionError

__all__ = [
    "DEFAULT_MAX_STATION_NUM",
    "MAX_STATION_NUM",
    "SolemClient",
    "SolemConnectionError",
    "APIConnectionError",
    "FirmwareVersion",
    "is_command_notification",
    "pack_commit",
    "pack_get_firmware_version",
    "pack_run_program",
    "pack_sprinkle_all_stations",
    "pack_sprinkle_station",
    "pack_stop_manual_sprinkle",
    "pack_turn_off_permanent",
    "pack_turn_off_x_days",
    "pack_turn_on",
    "parse_battery_9v",
    "parse_firmware_version_response",
    "parse_status_notification",
]
