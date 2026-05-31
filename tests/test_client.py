"""Unit tests for Solem BL-IP client BLE exchanges."""

import json
from pathlib import Path
from typing import Any

from solem_blip_ble.client import SolemClient

CAPTURE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "solem_metadata_c8b961d44dcc8.jsonl"
)


def _load_capture_notifications(probe: str) -> list[bytes]:
    payloads: list[bytes] = []
    for line in CAPTURE_FIXTURE.read_text().splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event["probe"] == probe and event["direction"] == "RX":
            payloads.append(bytes.fromhex(event["payload_hex"]))
    return payloads


class FakeBleakClient:
    is_connected = True

    def __init__(self) -> None:
        self.handler = None
        self.writes: list[bytes] = []

    async def start_notify(self, _uuid: str, handler) -> None:
        self.handler = handler

    async def stop_notify(self, _uuid: str) -> None:
        self.handler = None

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        self.handler(
            1,
            bytearray.fromhex("3512010046726f6e74206c61776e00000000000000"),
        )
        self.handler(
            1,
            bytearray.fromhex("351200002065617374000000000000000000000000"),
        )


class FakeFirmwareBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        self.handler(
            1,
            bytearray.fromhex("0f00010000000000000000000501050000"),
        )


class CaptureFirmwareBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        self.handler(1, bytearray(_load_capture_notifications("firmware")[0]))


class CaptureStationNamesBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        for notification in _load_capture_notifications("output_names")[:12]:
            self.handler(1, bytearray(notification))


async def test_get_station_name_reads_two_fragments_without_commit(monkeypatch):
    fake_client = FakeBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF", max_station_num=1)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_name(1) == "Front lawn east"
    assert fake_client.writes == [bytes.fromhex("3500")]


async def test_get_firmware_version_reads_identification_without_commit(monkeypatch):
    fake_client = FakeFirmwareBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF")

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_firmware_version() == {
        "major": 5,
        "minor": 1,
        "patch": 5,
        "raw_hex": "5.1.5",
    }
    assert fake_client.writes == [bytes.fromhex("0f00")]


async def test_get_firmware_version_from_capture(monkeypatch):
    fake_client = CaptureFirmwareBleakClient()
    client = SolemClient("C8:B9:61:D4:4D:C8")

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_firmware_version() == {
        "major": 5,
        "minor": 1,
        "patch": 7,
        "raw_hex": "5.1.7",
    }
    assert fake_client.writes == [bytes.fromhex("0f00")]


async def test_get_station_names_from_capture(monkeypatch):
    fake_client = CaptureStationNamesBleakClient()
    client = SolemClient("C8:B9:61:D4:4D:C8", max_station_num=6)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_names() == {
        1: "Eleagnus",
        2: "Stazione 2",
        3: "Stazione 3",
        4: "Stazione 4",
        5: "Vasi",
        6: "Stazione 6",
    }
    assert fake_client.writes == [bytes.fromhex("3500")]


async def test_get_station_names_uses_configured_station_count():
    client = SolemClient("AA:BB:CC:DD:EE:FF", mock=True, max_station_num=2)

    assert await client.get_station_names() == {
        1: "Station 1",
        2: "Station 2",
    }
