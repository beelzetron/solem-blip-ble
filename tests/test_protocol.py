"""Unit tests for Solem BL-IP protocol helpers."""

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
    # Matches validated controller api.py struct pack
    assert protocol.pack_turn_on() == bytes.fromhex("3105a000010000")


def test_pack_turn_off():
    assert protocol.pack_turn_off_permanent() == bytes.fromhex("3105c000000000")


def test_pack_turn_off_x_days_clamped_to_15():
    assert protocol.pack_turn_off_x_days(3) == bytes.fromhex("3105c000030000")
    # pcman75 documents max 15 days
    assert protocol.pack_turn_off_x_days(99) == bytes.fromhex("3105c0000f0000")


def test_pack_commit():
    assert protocol.pack_commit() == bytes.fromhex("3b00")


def test_parse_status_on_idle():
    # Minimal 18-byte frame: seq=2, status=0x40 (on, not watering)
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x40
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["controller_state"] == "On"
    assert parsed["is_watering"] is False
    assert parsed["station_num"] is None
    assert parsed["remaining_seconds"] is None
    assert parsed["battery_voltage"] is None
    assert parsed["battery_level"] is None
    assert parsed["battery_low"] is False


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


def test_parse_remaining_ignores_padding_at_14_16():
    # Real HCI capture: rem=60 at bytes 13-14; bytes 14-16 would read 0x3c10 (wrong)
    data = bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000")
    parsed = protocol.parse_status_notification(data)
    assert parsed is not None
    assert parsed["is_watering"] is True
    assert parsed["station_num"] == 1
    assert parsed["remaining_seconds"] == 60
