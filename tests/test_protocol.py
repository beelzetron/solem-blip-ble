"""Unit tests for Solem BL-IP protocol helpers."""

from datetime import datetime

import pytest

from solem_blip_ble import protocol


def test_pack_sprinkle_station_1_180s():
    assert protocol.pack_sprinkle_station(1, 3) == bytes.fromhex("310512010000b4")


def test_pack_sprinkle_station_1_60s():
    assert protocol.pack_sprinkle_station(1, 1) == bytes.fromhex("3105120100003c")


def test_is_command_notification():
    assert protocol.is_command_notification(bytearray.fromhex("3210024200"))
    assert protocol.is_command_notification(bytearray.fromhex("3210010000"))
    assert not protocol.is_command_notification(bytearray([0x32, 0x10, 0x05]))


def test_pack_stop_manual():
    assert protocol.pack_stop_manual_sprinkle() == bytes.fromhex("31051500ff0000")


def test_pack_turn_on():
    # V5 manual-on command with no station-specific parameter.
    assert protocol.pack_turn_on() == bytes.fromhex("3105a000000000")


def test_pack_turn_off():
    assert protocol.pack_turn_off_permanent() == bytes.fromhex("3105c000000000")


def test_pack_turn_off_x_days():
    assert protocol.pack_turn_off_x_days(3) == bytes.fromhex("3105c000030000")


@pytest.mark.parametrize(
    ("packer", "args"),
    [
        (protocol.pack_sprinkle_station, (0, 1)),
        (protocol.pack_sprinkle_station, (9, 1)),
        (protocol.pack_sprinkle_station, (1, 0)),
        (protocol.pack_sprinkle_station, (1, 241)),
        (protocol.pack_sprinkle_all_stations, (0,)),
        (protocol.pack_sprinkle_all_stations, (241,)),
        (protocol.pack_run_program, (0,)),
        (protocol.pack_run_program, (4,)),
        (protocol.pack_turn_off_x_days, (-1,)),
        (protocol.pack_turn_off_x_days, (16,)),
    ],
)
def test_write_packers_reject_out_of_range_values(packer, args):
    with pytest.raises(ValueError):
        packer(*args)


def test_pack_commit():
    assert protocol.pack_commit() == bytes.fromhex("3b00")


def test_pack_set_time():
    moment = datetime(2026, 5, 31, 22, 46, 14)
    assert protocol.pack_set_time(moment) == bytes.fromhex("0306007e051f162e0e")


def test_parse_status_on_idle():
    # Minimal 18-byte frame: seq=2, status=0x40 (on, not watering)
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x40
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["controller_state"] == "On"
    assert parsed["controller_off_mode"] == "on"
    assert parsed["controller_off_days_remaining"] == 0
    assert parsed["is_watering"] is False
    assert parsed["station_num"] is None
    assert parsed["remaining_seconds"] is None
    assert parsed["battery_voltage"] is None
    assert parsed["battery_level"] is None
    assert parsed["battery_low"] is False


def test_parse_status_off_for_days():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x00
    data[4] = 0x03
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["controller_state"] == "Off"
    assert parsed["controller_off_mode"] == "temporary"
    assert parsed["controller_off_days_remaining"] == 3


def test_parse_status_off_permanent_without_delay():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x00
    data[4] = 0x00
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["controller_state"] == "Off"
    assert parsed["controller_off_mode"] == "permanent"
    assert parsed["controller_off_days_remaining"] == 0


def test_parse_status_off_ignores_out_of_range_delay_bits():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x00
    data[4] = 0x3F
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["controller_off_mode"] == "permanent"
    assert parsed["controller_off_days_remaining"] == 0


def test_parse_status_watering_station_1():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x42  # on + watering
    data[9] = 1
    data[13] = 0x00
    data[14] = 0xB4  # 180 seconds (bytes 13-14, big-endian)
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is True
    assert parsed["station_num"] == 1
    assert parsed["remaining_seconds"] == 180
    assert parsed["battery_voltage"] is None


def test_battery_level_9v():
    assert protocol.battery_level_9v(59) == 0
    assert protocol.battery_level_9v(64) == 1
    assert protocol.battery_level_9v(79) == 4
    assert protocol.battery_level_9v(80) == 5


def test_parse_battery_from_hci_capture():
    data = bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["battery_voltage"] == 0x4F
    assert parsed["battery_level"] == 4
    assert parsed["battery_low"] is False


def test_parse_ignores_wrong_sequence():
    data = bytearray(18)
    data[2] = 0x01
    assert protocol.parse_status_notification(data) is None


def test_parse_period_start_date_from_header_bytes():
    from datetime import date

    normalized = bytearray(20)
    normalized[12] = 1
    normalized[13] = 6
    normalized[14:16] = b"\x07\xea"
    assert protocol.parse_period_start_date(normalized) == date(2026, 6, 1)


def test_parse_status_program_run_uses_0x44():
    # Hardware capture after run_program A: 0x44 + byte8=1 + 1500s remaining
    data = bytearray.fromhex("32100244006aaaaa01014f101005dc100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is True
    assert parsed["active_program"] == 1
    assert parsed["station_num"] == 1
    assert parsed["remaining_seconds"] == 1500
    assert parsed["watering_origin"] == "program"


def test_parse_status_program_b_watering_uses_byte_8():
    """Program B (Vasi): 0x42 watering with byte 8=2 is a program run, not manual."""
    data = bytearray.fromhex("3c10024200aaaaaa02054f11100000100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is True
    assert parsed["active_program"] == 2
    assert parsed["station_num"] == 5
    assert parsed["watering_origin"] == "program"


def test_parse_active_program_during_inter_station_idle():
    """Program index in byte 8 persists when controller is ON but not watering."""
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x40
    data[8] = 1
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is False
    assert parsed["active_program"] == 1
    assert parsed["watering_origin"] == "program"
    assert parsed["station_num"] is None


def test_parse_active_program_from_status_byte_8():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x42
    data[8] = 3
    data[9] = 2
    data[10] = 0x4F
    data[12] = 0x00
    data[13] = 0x05
    data[14] = 0xDC
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["active_program"] == 3
    assert parsed["watering_origin"] == "program"


def test_parse_active_program_zero_for_station_manual():
    data = bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["active_program"] is None


def test_parse_remaining_ignores_padding_at_14_16():
    # Real HCI capture: rem=60 at bytes 12-14 (int3) and 13-14 (uint16); byte 15+ is padding
    data = bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is True
    assert parsed["station_num"] == 1
    assert parsed["remaining_seconds"] == 60


def test_parse_remaining_station_2_slot():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x42
    data[9] = 2
    data[15] = 0x00
    data[16] = 0x00
    data[17] = 0xB4  # 180 seconds in station-2 slot (bytes 15-17)
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["station_num"] == 2
    assert parsed["remaining_seconds"] == 180


def test_parse_remaining_station_5_int3_at_12_14():
    # Same layout as station 1 HCI capture but active station 5
    data = bytearray.fromhex("3210024200aaaaaa00054f0c10003c100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["station_num"] == 5
    assert parsed["remaining_seconds"] == 60


def test_parse_intermediate_remaining_station_5():
    data = bytearray(18)
    data[2] = 0x01
    data[9] = 0x00
    data[10] = 0x01
    data[11] = 0x2C  # 300 seconds at offset (5 - 3) * 3 + 3
    assert protocol.parse_intermediate_remaining(data, 5) == 300


def test_parse_intermediate_remaining_ignores_seq_2():
    data = bytearray(18)
    data[2] = 0x02
    assert protocol.parse_intermediate_remaining(data, 5) is None


def test_pack_get_firmware_version():
    """Test firmware version query command packing."""
    assert protocol.pack_get_firmware_version() == bytes.fromhex("0f00")


def test_pack_get_station_names():
    assert protocol.pack_get_station_names() == bytes.fromhex("3500")


def test_parse_station_name_fragments():
    first = bytearray.fromhex("3512010046726f6e74206c61776e00000000000000")
    last = bytearray.fromhex("351200002065617374000000000000000000000000")

    first_parsed = protocol.parse_station_name_fragment(first)
    last_parsed = protocol.parse_station_name_fragment(last)

    assert first_parsed == {
        "station": 1,
        "sequence": 1,
        "name_bytes": b"Front lawn",
    }
    assert last_parsed == {
        "station": 1,
        "sequence": 0,
        "name_bytes": b" east",
    }
    assert (
        first_parsed["name_bytes"] + last_parsed["name_bytes"]
    ).decode() == "Front lawn east"


def test_parse_station_name_fragment_rejects_other_frames():
    assert protocol.parse_station_name_fragment(bytearray.fromhex("3b00")) is None
    assert protocol.parse_station_name_fragment(bytearray.fromhex("35000200")) is None


def test_parse_firmware_version_response_valid():
    """Parse identification response with firmware version 5.1.5.

    Based on the V5 identification response layout:
    - Byte 0: Command (0x0F)
    - Bytes 12-14: Firmware major, minor, and patch version
    """
    # Simulated response: MAC + HW info + firmware v5.1.5
    data = bytearray(17)
    data[0] = 0x0F  # CMD_ID_V2
    data[1] = 0x00  # Subcommand
    data[2] = 0x01  # Response type (identification)
    # Bytes 3-8: MAC address (placeholder)
    data[3] = 0x10
    data[4] = 0x8B
    data[5] = 0x00
    data[6] = 0x01
    data[7] = 0xE0
    data[8] = 0x00
    # Bytes 9-11: Hardware info
    data[9] = 0x09
    data[10] = 0xD0
    data[11] = 0x46
    # Bytes 12-14: Firmware version (5.1.5)
    data[12] = 0x05
    data[13] = 0x01
    data[14] = 0x05
    # Bytes 15-16: Serial number
    data[15] = 0xE8
    data[16] = 0x0B

    result = protocol.parse_firmware_version_response(data)
    assert result is not None
    assert result["major"] == 5
    assert result["minor"] == 1
    assert result["patch"] == 5
    assert result["raw_hex"] == "5.1.5"


def test_parse_firmware_version_response_v6():
    """Test parsing firmware version 6.x."""
    data = bytearray(17)
    data[0] = 0x0F
    data[1] = 0x00
    data[2] = 0x01
    data[12] = 0x06
    data[13] = 0x12  # v6.12
    data[14] = 0x03

    result = protocol.parse_firmware_version_response(data)
    assert result is not None
    assert result["major"] == 6
    assert result["minor"] == 18
    assert result["patch"] == 3
    assert result["raw_hex"] == "6.18.3"


def test_parse_firmware_version_invalid_command():
    """Reject responses with wrong command code."""
    data = bytearray(17)
    data[0] = 0x3B  # Not CMD_ID
    data[1] = 0x00
    data[2] = 0x00
    data[12] = 0x05
    data[13] = 0x00

    assert protocol.parse_firmware_version_response(data) is None


def test_parse_firmware_version_invalid_subtype():
    """Reject responses with non-identification subcommand."""
    data = bytearray(17)
    data[0] = 0x0F
    data[1] = 0x00
    data[2] = 0x00  # Not identification (should be 0x01)
    data[12] = 0x05
    data[13] = 0x00

    assert protocol.parse_firmware_version_response(data) is None


def test_parse_firmware_version_too_short():
    """Reject responses shorter than 16 bytes."""
    data = bytearray(10)
    data[0] = 0x01
    data[1] = 0x00
    data[2] = 0x00

    assert protocol.parse_firmware_version_response(data) is None
