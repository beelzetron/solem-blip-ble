"""Tests for the device-derived station count (#57).

Covers:

- :class:`StationNameSnapshot.station_count` / ``reported_count``
  (station_names.py).
- ``StatelessSolemClient`` adoption: after a full station-name read the
  client sizes user-facing status parsing with the device-reported count,
  keeping ``max_station_num`` as the fallback before the first name read.
"""

from __future__ import annotations

from typing import Any

from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.station_names import StationNameSnapshot


# -- snapshot properties ---------------------------------------------------


def test_station_count_is_highest_named_output():
    snapshot = StationNameSnapshot(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
        | {i: bytes(32) for i in range(7, 13)}
    )
    assert snapshot.station_count == 6


def test_station_count_all_empty_falls_back_to_max_key():
    snapshot = StationNameSnapshot({i: bytes(32) for i in range(1, 9)})
    assert snapshot.station_count == 8


def test_station_count_single_frame():
    snapshot = StationNameSnapshot({1: b"Garden".ljust(32, b"\0")})
    assert snapshot.station_count == 1


def test_station_count_gap_keeps_highest_named():
    # Station 3 named, station 2 empty: the count is 3 — the highest
    # named output wins, gaps below do not lower it.
    snapshot = StationNameSnapshot(
        {
            1: bytes(32),
            2: bytes(32),
            3: b"Trees".ljust(32, b"\0"),
        }
    )
    assert snapshot.station_count == 3


def test_reported_count_is_raw_number_of_reported_outputs():
    snapshot = StationNameSnapshot(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
        | {i: bytes(32) for i in range(7, 13)}
    )
    assert snapshot.reported_count == 12
    assert snapshot.station_count == 6


def test_reported_count_matches_station_count_when_all_named():
    snapshot = StationNameSnapshot(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
    )
    assert snapshot.reported_count == 6
    assert snapshot.station_count == 6


def test_station_count_whitespace_only_name_counts_as_named():
    # Deliberate choice: a whitespace-only name is still device-held data,
    # so slot 7 counts toward station_count (see station_names.py docstring).
    snapshot = StationNameSnapshot(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
        | {7: b"  ".ljust(32, b"\0")}
        | {i: bytes(32) for i in range(8, 13)}
    )
    assert snapshot.station_count == 7


# -- connect-time adoption -------------------------------------------------


def _name_frames(raw_names: dict[int, bytes]) -> list[bytes]:
    frames = []
    total = len(raw_names)
    for station, raw in sorted(raw_names.items()):
        seq = (total - station) * 2
        frames += [
            bytes([0x36, 0x12, seq + 1, station - 1]) + raw[:16],
            bytes([0x36, 0x12, seq, station - 1]) + raw[16:],
        ]
    return frames


class _SnapshotNameSession:
    """Fake bleak client answering the 0x35 name read and 0x3b commit."""

    is_connected = True

    def __init__(self, frames: list[bytes], station_num: int = 6) -> None:
        self.frames = frames
        self.station_num = station_num
        self.handler: Any = None
        self.writes: list[bytes] = []

    @property
    def status_frame(self) -> bytearray:
        # seq=0x02 status with watering bits (0x42) and the given station.
        frame = bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000")
        frame[9] = self.station_num
        return frame

    async def start_notify(self, _uuid: str, handler) -> None:
        self.handler = handler

    async def stop_notify(self, _uuid: str) -> None:
        self.handler = None

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        if payload == b"\x35\x00" and self.handler is not None:
            for frame in self.frames:
                self.handler(1, bytearray(frame))
        elif payload == b"\x3b\x00" and self.handler is not None:
            self.handler(1, self.status_frame)


def _patch_connection(monkeypatch, fake) -> None:
    from solem_blip_ble import client_v2

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        return fake

    monkeypatch.setattr(
        StatelessSolemClient, "_resolve_ble_device", fake_resolve
    )
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)


async def test_after_name_read_status_uses_device_station_count(
    monkeypatch,
) -> None:
    """The device reports 6 named outputs; a status frame for station 6
    must be accepted after the name read even with max_station_num=4."""
    frames = _name_frames(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
    )
    fake = _SnapshotNameSession(frames)
    _patch_connection(monkeypatch, fake)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", max_station_num=4)
    await client.get_station_name_snapshot()

    assert client.station_count == 6
    status = await client.get_status()
    assert status["station_num"] == 6
    assert status["is_watering"] is True


async def test_before_name_read_status_falls_back_to_max_station_num(
    monkeypatch,
) -> None:
    """Before any name read, max_station_num remains the effective bound:
    station 6 is rejected with max_station_num=4."""
    fake = _SnapshotNameSession([])
    _patch_connection(monkeypatch, fake)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", max_station_num=4)
    assert client.station_count == 4

    status = await client.get_status()
    assert status["station_num"] is None
    assert status["is_watering"] is True  # activity bits only


async def test_unnamed_outputs_do_not_inflate_adopted_count(
    monkeypatch,
) -> None:
    """The device reports all 8 outputs but only 6 have names: the adopted
    count is 6, not the raw reported width."""
    frames = _name_frames(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
        | {i: bytes(32) for i in range(7, 9)}
    )
    fake = _SnapshotNameSession(frames)
    _patch_connection(monkeypatch, fake)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", max_station_num=8)
    await client.get_station_name_snapshot()

    assert client.station_count == 6

    # User-facing sizing uses the adopted count...
    status = await client.get_status()
    assert status["station_num"] == 6
    assert status["is_watering"] is True

    # ...and the extra unnamed outputs stay captured in raw_names
    # (from_frames accepts outputs beyond the validation width) without
    # inflating the adopted count: the readback snapshot reports 8
    # outputs, of which 6 are named.
    readback = await client.get_station_name_snapshot()
    assert readback.reported_count == 8
    assert readback.station_count == 6
    assert client.station_count == 6


async def test_rename_of_empty_slot_bumps_station_count(monkeypatch) -> None:
    """Post-rename refresh re-adopts the device count: naming the highest
    empty slot via write_station_name bumps client.station_count from 6
    to 7 (max_station_num stays 12)."""
    before = StationNameSnapshot(
        {i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)}
        | {i: bytes(32) for i in range(7, 13)}
    )
    expected = before.renamed(7, "Hedge", 12)
    fake = _SnapshotNameSession(_name_frames(before.raw_names), station_num=6)
    _patch_connection(monkeypatch, fake)

    async def swap_after_write(_uuid, payload, *, response):
        fake.writes.append(payload)
        if payload[:1] == b"\x33":
            fake.frames = _name_frames(expected.raw_names)
            fake.handler(1, bytearray(b"\x34" + payload[1:4]))
            return
        if payload == b"\x35\x00":
            for frame in fake.frames:
                fake.handler(1, bytearray(frame))
            return
        fake.handler(1, fake.status_frame)

    fake.write_gatt_char = swap_after_write

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", max_station_num=12)
    await client.get_station_name_snapshot()
    assert client.station_count == 6

    actual = await client.write_station_name(
        7, "Hedge", expected, before=before
    )
    assert actual.station_count == 7
    assert client.station_count == 7
