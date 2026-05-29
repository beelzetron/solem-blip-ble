"""Unit tests for Solem BL-IP protocol helpers."""

from solem_blip_ble import protocol


def test_pack_sprinkle_station_1_180s():
    # 310512010000b4 from test_findings_summary
    assert protocol.pack_sprinkle_station(1, 3) == bytes.fromhex("310512010000b4")


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
