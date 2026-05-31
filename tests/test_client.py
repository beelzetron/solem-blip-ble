"""Unit tests for Solem BL-IP client BLE exchanges."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from solem_blip_ble.client import SolemClient
from solem_blip_ble import protocol
from solem_blip_ble.exceptions import SolemConnectionError

CAPTURE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "solem_metadata_c8b961d44dcc8.jsonl"
)
IRRIGATION_CAPTURE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "solem_irrigation_config_c8b961d44dcc8.jsonl"
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


class FakeWriteOnlyBleakClient:
    is_connected = True

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False


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


class CaptureIrrigationConfigBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        for line in IRRIGATION_CAPTURE_FIXTURE.read_text().splitlines():
            if not line:
                continue
            event = json.loads(line)
            if event["probe"] == "irrigation_config" and event["direction"] == "RX":
                self.handler(1, bytearray.fromhex(event["payload_hex"]))


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


async def test_set_time_writes_without_commit(monkeypatch):
    fake_client = FakeWriteOnlyBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    moment = datetime(2026, 5, 31, 22, 46, 14)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    await client.set_time(moment)

    assert fake_client.writes == [protocol.pack_set_time(moment)]
    assert all(write != protocol.pack_commit() for write in fake_client.writes)


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


async def test_get_irrigation_config_from_capture(monkeypatch):
    fake_client = CaptureIrrigationConfigBleakClient()
    client = SolemClient("C8:B9:61:D4:4D:C8", max_station_num=12)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    programs = await client.get_irrigation_config()
    assert programs[0]["name"] == "Programma A"
    assert programs[1]["name"] == "Programma B"
    assert programs[2]["name"] == "Programma C"
    assert programs[0]["start_times"][0] == 1060
    assert programs[2]["start_times"][0] == 270
    assert programs[2]["station_durations"][1:4] == [1500, 1500, 1500]
    assert fake_client.writes == [bytes.fromhex("3900")]


async def test_get_station_names_uses_configured_station_count():
    client = SolemClient("AA:BB:CC:DD:EE:FF", mock=True, max_station_num=2)

    assert await client.get_station_names() == {
        1: "Station 1",
        2: "Station 2",
    }


async def test_concurrent_operations_execute_sequentially(monkeypatch):
    fake_client = FakeWriteOnlyBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def get_connected_client():
        return fake_client

    async def first_operation(_client):
        calls.append("first-start")
        first_started.set()
        await release_first.wait()
        calls.append("first-stop-notify")

    async def second_operation(_client):
        calls.append("second-start")

    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)

    first_task = asyncio.create_task(client._run_with_client(first_operation))
    await first_started.wait()
    second_task = asyncio.create_task(client._run_with_client(second_operation))
    await asyncio.sleep(0)

    assert calls == ["first-start"]

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert calls == ["first-start", "first-stop-notify", "second-start"]


async def test_disconnect_waits_for_active_operation(monkeypatch):
    fake_client = FakeWriteOnlyBleakClient()
    fake_client.disconnect_called = False

    async def disconnect():
        fake_client.disconnect_called = True

    fake_client.disconnect = disconnect
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    client._client = fake_client
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()

    async def get_connected_client():
        return fake_client

    async def operation(_client):
        operation_started.set()
        await release_operation.wait()

    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)

    operation_task = asyncio.create_task(client._run_with_client(operation))
    await operation_started.wait()
    disconnect_task = asyncio.create_task(client.disconnect())
    await asyncio.sleep(0)

    assert fake_client.disconnect_called is False

    release_operation.set()
    await asyncio.gather(operation_task, disconnect_task)

    assert fake_client.disconnect_called is True


async def test_resolver_does_not_fall_back_to_standalone_scanner(monkeypatch):
    client = SolemClient(
        "AA:BB:CC:DD:EE:FF",
        ble_device_resolver=lambda: None,
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("standalone scanner must not be used with a resolver")

    monkeypatch.setattr(
        "solem_blip_ble.client.BleakScanner.find_device_by_address",
        fail_if_called,
    )
    monkeypatch.setattr(
        "solem_blip_ble.client.BleakScanner.discover",
        fail_if_called,
    )

    try:
        await client._resolve_ble_device()
    except SolemConnectionError:
        pass
    else:
        raise AssertionError("missing resolver device must fail")
