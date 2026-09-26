"""Client-level tests for name commands (issues #98/#99 BE side).

Ported from the fork's tests (ThomasHFWright/solem-blip-ha PR #1,
tests/test_controller_name.py + tests/test_station_names.py client
cases), adapted to the solem_blip_ble StatelessSolemClient architecture.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from solem_blip_ble import client_v2, protocol
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import StaleProgram, UncertainWrite
from solem_blip_ble.station_names import StationNameSnapshot

from test_client_v2 import FakeV2Client

ADDRESS = "AA:BB:CC:DD:EE:FF"
FIRMWARE = bytes.fromhex("100f010000000000000000000501070000")
NAME = b"\x10\x10\x00" + b"Garden unit".ljust(15, b"\0")


@pytest.mark.parametrize(
    "order", ["name_first", "firmware_first", "missing", "malformed", "cancel"]
)
async def test_identification_uses_one_session_and_optional_name(
    established, monkeypatch, order
):
    """One identification session; the name record is optional metadata."""
    fake = FakeV2Client()
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "IDENTIFICATION_NAME_TIMEOUT", 0.02)
    sent_firmware = asyncio.Event()
    pending: list[asyncio.Task[None]] = []

    async def write(_uuid, payload, *, response):
        fake.writes.append(payload)
        assert payload == b"\x0f\x00"
        if order == "name_first":
            fake.handler(1, bytearray(NAME))
        fake.handler(1, bytearray(FIRMWARE))
        sent_firmware.set()
        if order in ("firmware_first", "malformed"):

            async def delayed_name() -> None:
                await asyncio.sleep(0.001)
                fake.handler(
                    1,
                    bytearray(
                        NAME if order == "firmware_first" else NAME[:-1]
                    ),
                )

            pending.append(asyncio.create_task(delayed_name()))

    async def connect(self):
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_connect", connect)
    fake.write_gatt_char = write
    client = StatelessSolemClient(ADDRESS)
    task = asyncio.create_task(client.get_firmware_version())
    if order == "cancel":
        await sent_firmware.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["raw_hex"] == "5.1.7"
        assert result.get("controller_name") == (
            "Garden unit" if order in ("name_first", "firmware_first") else None
        )
    await asyncio.gather(*pending)
    assert fake.writes == [b"\x0f\x00"]  # Never commit or write names.
    assert fake.disconnects == 1
    assert not fake.is_connected


def _snapshot(raw_names: dict[int, bytes]) -> StationNameSnapshot:
    return StationNameSnapshot(raw_names)


def _frames_for(snapshot: StationNameSnapshot) -> list[bytes]:
    frames = []
    for station, raw in sorted(snapshot.raw_names.items()):
        seq = (len(snapshot.raw_names) - station) * 2
        frames += [
            bytes([0x36, 0x12, seq + 1, station - 1]) + raw[:16],
            bytes([0x36, 0x12, seq, station - 1]) + raw[16:],
        ]
    return frames


class _NameSession(FakeV2Client):
    """Fake client that streams name frames and write acks on one connection."""

    def __init__(self, read_frames: list[bytes]) -> None:
        super().__init__()
        self.read_frames = read_frames
        self.mode = "idle"

    async def write_gatt_char(self, _uuid, payload, *, response) -> None:
        self.writes.append(payload)
        if payload == protocol.pack_get_station_names():
            self.mode = "read"
            for frame in self.read_frames:
                self.handler(1, bytearray(frame))
            return
        if payload[:1] == b"\x33":
            self.mode = "write"
            self.handler(1, bytearray(b"\x34" + payload[1:4]))
            return
        self.mode = "idle"


def _patch_connection(monkeypatch, fake: _NameSession) -> None:
    async def connect(self):
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_connect", connect)


@pytest.fixture
def established(monkeypatch):
    """Patch resolution/connection so each operation uses a fresh fake.

    Mirrors the fixture in tests/test_client_v2.py without importing it
    (test modules are not a package in this suite).
    """

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        return _NameSession([])

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)


async def test_write_station_name_preflight_write_readback(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    expected = before.renamed(2, "Greenhouse", 6)
    fake = _NameSession(_frames_for(before))
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)

    async def swap_after_write(_uuid, payload, *, response):
        fake.writes.append(payload)
        if payload[:1] == b"\x33":
            fake.read_frames = _frames_for(expected)
            fake.mode = "write"
            fake.handler(1, bytearray(b"\x34" + payload[1:4]))
            return
        if payload == protocol.pack_get_station_names():
            fake.mode = "read"
            for frame in fake.read_frames:
                fake.handler(1, bytearray(frame))
            return
        fake.mode = "idle"

    fake.write_gatt_char = swap_after_write
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    actual = await client.write_station_name(
        2, "Greenhouse", expected, before=before
    )
    assert actual.revision == expected.revision
    assert fake.writes[0] == protocol.pack_get_station_names()  # preflight read
    written = [f for f in fake.writes if f[:1] == b"\x33"]
    assert written == protocol.pack_station_name(2, "Greenhouse", 6)
    assert fake.writes[-1] == protocol.pack_get_station_names()  # readback
    assert client.station_name_write_diagnostics["phase"] == "verified"
    assert client.station_name_write_diagnostics["acknowledged_parts"] == 2


async def test_write_station_name_stale_preflight_never_writes(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    changed = before.renamed(5, "Changed elsewhere", 6)
    fake = _NameSession(_frames_for(changed))
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    with pytest.raises(StaleProgram):
        await client.write_station_name(2, "Greenhouse", before, before=before)
    assert not [f for f in fake.writes if f[:1] == b"\x33"]


async def test_write_station_name_rejection_raises_uncertain(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    fake = _NameSession(_frames_for(before))

    async def reject_writes(_uuid, payload, *, response):
        fake.writes.append(payload)
        if payload[:1] == b"\x33":
            fake.handler(1, bytearray(b"\x34\x12\xf0\x01"))
            return
        if payload == protocol.pack_get_station_names():
            for frame in fake.read_frames:
                fake.handler(1, bytearray(frame))
            return

    fake.write_gatt_char = reject_writes
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    with pytest.raises(UncertainWrite):
        await client.write_station_name(
            2, "Greenhouse", before.renamed(2, "Greenhouse", 6), before=before
        )
    assert len([f for f in fake.writes if f[:1] == b"\x33"]) == 1


async def test_write_station_name_readback_mismatch_raises_uncertain(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    expected = before.renamed(2, "Greenhouse", 6)
    partial = before.renamed(2, "Green", 6)
    fake = _NameSession(_frames_for(before))
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)

    async def partial_write(_uuid, payload, *, response):
        fake.writes.append(payload)
        if payload[:1] == b"\x33":
            # Controller applied only a truncated name: readback mismatch.
            fake.read_frames = _frames_for(partial)
            fake.handler(1, bytearray(b"\x34" + payload[1:4]))
            return
        if payload == protocol.pack_get_station_names():
            for frame in fake.read_frames:
                fake.handler(1, bytearray(frame))
            return

    fake.write_gatt_char = partial_write
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    with pytest.raises(UncertainWrite):
        await client.write_station_name(2, "Greenhouse", expected, before=before)


async def test_get_station_name_snapshot_rejects_incomplete(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    frames = _frames_for(before)
    fake = _NameSession(frames[:-1])  # one half missing
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    with pytest.raises(Exception):  # InvalidSnapshot via connection error path
        await client.get_station_name_snapshot()


async def test_get_station_name_snapshot_returns_verified_snapshot(
    established, monkeypatch
):
    before = _snapshot({i: f"S{i}".encode().ljust(32, b"\0") for i in range(1, 7)})
    fake = _NameSession(_frames_for(before))
    _patch_connection(monkeypatch, fake)
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "STATION_NAMES_IDLE_TIMEOUT", 0)
    client = StatelessSolemClient(ADDRESS, max_station_num=6)
    actual = await client.get_station_name_snapshot()
    assert actual.revision == before.revision
    assert actual.names == before.names


async def test_mock_write_station_name_applies_expected():
    client = StatelessSolemClient(ADDRESS, mock=True, max_station_num=6)
    before = await client.get_station_name_snapshot()
    expected = before.renamed(3, "Vasi", 6)
    actual = await client.write_station_name(3, "Vasi", expected, before=before)
    assert actual.revision == expected.revision
    assert (await client.get_station_name_snapshot()).names[3] == "Vasi"
