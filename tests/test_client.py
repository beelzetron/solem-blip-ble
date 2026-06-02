"""Unit tests for Solem BL-IP client BLE exchanges."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

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


class FakeCharacteristic:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class FakeServices:
    def __init__(self) -> None:
        self.write = FakeCharacteristic("108b0002-eab5-bc09-d0ea-0b8f467ce8ee")
        self.notify = FakeCharacteristic("108b0003-eab5-bc09-d0ea-0b8f467ce8ee")

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        by_uuid = {
            self.write.uuid: self.write,
            self.notify.uuid: self.notify,
        }
        return by_uuid.get(uuid)

    def __iter__(self):
        return iter(())


class FakeRawBleakClient(FakeWriteOnlyBleakClient):
    def __init__(self) -> None:
        super().__init__()
        self.services = FakeServices()
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


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


class FakePartialStationNameBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        self.handler(
            1,
            bytearray.fromhex("351200002065617374000000000000000000000000"),
        )


class FakeReversedStationNameBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        self.handler(
            1,
            bytearray.fromhex("351200002065617374000000000000000000000000"),
        )
        self.handler(
            1,
            bytearray.fromhex("3512010046726f6e74206c61776e00000000000000"),
        )


class FakeCommandBleakClient(FakeBleakClient):
    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        if payload == protocol.pack_commit():
            assert self.handler is not None
            self.handler(
                1,
                bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000"),
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


class CapturePartialStationNamesBleakClient(FakeBleakClient):
    """Deliver name fragments for the first four stations only."""

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None
        for notification in _load_capture_notifications("output_names")[:8]:
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


class StreamingStationNamesBleakClient(FakeBleakClient):
    """Deliver an unused output slot after the configured station name."""

    def __init__(self) -> None:
        super().__init__()
        self.tail_delivered = asyncio.Event()
        self.stopped_before_tail = False
        self.delivery_task: asyncio.Task[None] | None = None

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None

        async def deliver() -> None:
            assert self.handler is not None
            self.handler(
                1,
                bytearray.fromhex("3512010046726f6e74206c61776e00000000000000"),
            )
            self.handler(
                1,
                bytearray.fromhex("351200002065617374000000000000000000000000"),
            )
            await asyncio.sleep(0.01)
            assert self.handler is not None
            self.handler(1, bytearray.fromhex("3612030100000000000000000000000000000000"))
            self.tail_delivered.set()

        self.delivery_task = asyncio.create_task(deliver())

    async def stop_notify(self, uuid: str) -> None:
        self.stopped_before_tail = not self.tail_delivered.is_set()
        await super().stop_notify(uuid)


class StreamingIrrigationConfigBleakClient(FakeBleakClient):
    """Deliver an unused program slot after programs A/B/C are complete."""

    def __init__(self) -> None:
        super().__init__()
        self.tail_delivered = asyncio.Event()
        self.stopped_before_tail = False
        self.delivery_task: asyncio.Task[None] | None = None

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        assert self.handler is not None

        async def deliver() -> None:
            assert self.handler is not None
            delivered = 0
            for line in IRRIGATION_CAPTURE_FIXTURE.read_text().splitlines():
                if not line:
                    continue
                event = json.loads(line)
                if (
                    event["probe"] == "irrigation_config"
                    and event["direction"] == "RX"
                ):
                    self.handler(1, bytearray.fromhex(event["payload_hex"]))
                    delivered += 1
                    if delivered == 21:
                        break
            await asyncio.sleep(0.01)
            assert self.handler is not None
            self.handler(
                1,
                bytearray.fromhex("3a120d1a00000000000000000000000000000000"),
            )
            self.tail_delivered.set()

        self.delivery_task = asyncio.create_task(deliver())

    async def stop_notify(self, uuid: str) -> None:
        self.stopped_before_tail = not self.tail_delivered.is_set()
        await super().stop_notify(uuid)


async def test_get_station_name_reads_two_fragments_without_commit(monkeypatch):
    fake_client = FakeBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF", max_station_num=1)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_name(1) == "Front lawn east"
    assert fake_client.writes == [bytes.fromhex("3500")]


async def test_get_station_name_retries_when_first_fragment_is_missing(monkeypatch):
    fake_client = FakePartialStationNameBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF", max_station_num=1)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client.STATUS_NOTIFY_TIMEOUT", 0.001)
    monkeypatch.setattr("solem_blip_ble.client.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    with pytest.raises(SolemConnectionError, match="station names"):
        await client.get_station_name(1)


async def test_execute_command_waits_for_final_notification(monkeypatch):
    fake_client = FakeCommandBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF")

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    task = asyncio.create_task(client._execute_command(protocol.pack_turn_on()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done()
    assert fake_client.handler is not None
    fake_client.handler(1, bytearray.fromhex("3210000000"))

    status = await task
    assert status is not None
    assert status["is_watering"] is True


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


async def test_get_station_names_assembles_reversed_fragment_order(monkeypatch):
    fake_client = FakeReversedStationNameBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF", max_station_num=1)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_names() == {1: "Front lawn east"}
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


async def test_get_station_names_drains_unused_output_slots(monkeypatch):
    fake_client = StreamingStationNamesBleakClient()
    client = SolemClient("AA:BB:CC:DD:EE:FF", max_station_num=1)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client.STATION_NAMES_IDLE_TIMEOUT", 0.02)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_names() == {1: "Front lawn east"}
    assert fake_client.delivery_task is not None
    await fake_client.delivery_task
    assert not fake_client.stopped_before_tail


async def test_get_irrigation_config_drains_unused_program_slots(monkeypatch):
    fake_client = StreamingIrrigationConfigBleakClient()
    client = SolemClient("C8:B9:61:D4:4D:C8", max_station_num=6)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client.IRRIGATION_CONFIG_IDLE_TIMEOUT", 0.02)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    programs = await client.get_irrigation_config()
    assert set(programs) == {0, 1, 2}
    assert fake_client.delivery_task is not None
    await fake_client.delivery_task
    assert not fake_client.stopped_before_tail


async def test_get_station_names_uses_configured_station_count():
    client = SolemClient("AA:BB:CC:DD:EE:FF", mock=True, max_station_num=2)

    assert await client.get_station_names() == {
        1: "Station 1",
        2: "Station 2",
    }


async def test_get_station_names_returns_partial_after_idle(monkeypatch):
    fake_client = CapturePartialStationNamesBleakClient()
    client = SolemClient("C8:B9:61:D4:4D:C8", max_station_num=6)

    async def run_with_client(operation) -> Any:
        return await operation(fake_client)

    monkeypatch.setattr("solem_blip_ble.client.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client.STATION_NAMES_IDLE_TIMEOUT", 0.01)
    monkeypatch.setattr(client, "_run_with_client", run_with_client)

    assert await client.get_station_names() == {
        1: "Eleagnus",
        2: "Stazione 2",
        3: "Stazione 3",
        4: "Stazione 4",
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


async def test_operation_timeout_releases_lock(monkeypatch) -> None:
    """Hung BLE operations time out and release the operation lock."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()

    async def get_connected_client():
        return fake_client

    async def hang(_client):
        await asyncio.sleep(100)

    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)
    monkeypatch.setattr("solem_blip_ble.client.OPERATION_TIMEOUT", 0.05)

    with pytest.raises(SolemConnectionError, match="timed out"):
        await client._run_with_client(hang)

    async def ok(_client):
        return "ok"

    assert await client._run_with_client(ok) == "ok"


async def test_operation_timeout_releases_lock_when_disconnect_resists_cancellation(
    monkeypatch,
) -> None:
    """Hung disconnect cleanup cannot retain the operation lock."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    disconnect_cancelled = asyncio.Event()
    release_disconnect = asyncio.Event()

    async def disconnect():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            disconnect_cancelled.set()
            await release_disconnect.wait()

    async def get_connected_client():
        return fake_client

    async def hang(_client):
        await asyncio.sleep(100)

    async def ok(_client):
        return "ok"

    fake_client.disconnect = disconnect
    client._client = fake_client
    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)
    monkeypatch.setattr("solem_blip_ble.client.OPERATION_TIMEOUT", 0.05)
    monkeypatch.setattr("solem_blip_ble.client.DISCONNECT_TIMEOUT", 0.01)

    with pytest.raises(SolemConnectionError, match="timed out"):
        await client._run_with_client(hang)

    await disconnect_cancelled.wait()
    assert await client._run_with_client(ok) == "ok"
    release_disconnect.set()


async def test_operation_timeout_releases_lock_when_operation_resists_cancellation(
    monkeypatch,
) -> None:
    """Cancellation-resistant BLE cleanup cannot retain the operation lock."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    operation_cancelled = asyncio.Event()
    release_operation = asyncio.Event()

    async def get_connected_client():
        return fake_client

    async def resist_cancellation(_client):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            operation_cancelled.set()
            await release_operation.wait()

    async def ok(_client):
        return "ok"

    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)
    monkeypatch.setattr("solem_blip_ble.client.OPERATION_TIMEOUT", 0.01)

    with pytest.raises(SolemConnectionError, match="timed out"):
        await asyncio.wait_for(
            client._run_with_client(resist_cancellation),
            timeout=0.05,
        )

    await operation_cancelled.wait()
    assert await client._run_with_client(ok) == "ok"
    release_operation.set()


async def test_operation_timeout_releases_lock_when_connect_resists_cancellation(
    monkeypatch,
) -> None:
    """Cancellation-resistant connection setup cannot retain the connection lock."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    connect_cancelled = asyncio.Event()
    release_connect = asyncio.Event()
    establish_calls = 0

    async def disconnect():
        return None

    async def establish_ble_connection():
        nonlocal establish_calls
        establish_calls += 1
        if establish_calls == 1:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                connect_cancelled.set()
                await release_connect.wait()
        return fake_client

    async def ok(_client):
        return "ok"

    fake_client.disconnect = disconnect
    monkeypatch.setattr(client, "_establish_ble_connection", establish_ble_connection)
    monkeypatch.setattr("solem_blip_ble.client.OPERATION_TIMEOUT", 0.01)

    with pytest.raises(SolemConnectionError, match="timed out"):
        await client._run_with_client(ok)

    await connect_cancelled.wait()
    assert await client._run_with_client(ok) == "ok"
    release_connect.set()
    await asyncio.sleep(0)


async def test_external_cancellation_releases_lock_when_disconnect_hangs(
    monkeypatch,
) -> None:
    """HA-style wait_for cancellation cannot wedge later operations."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    disconnect_cancelled = asyncio.Event()
    release_disconnect = asyncio.Event()

    async def disconnect():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            disconnect_cancelled.set()
            await release_disconnect.wait()

    async def get_connected_client():
        return fake_client

    async def hang(_client):
        await asyncio.sleep(100)

    async def ok(_client):
        return "ok"

    fake_client.disconnect = disconnect
    client._client = fake_client
    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)
    monkeypatch.setattr("solem_blip_ble.client.DISCONNECT_TIMEOUT", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client._run_with_client(hang), timeout=0.05)

    await disconnect_cancelled.wait()
    assert await client._run_with_client(ok) == "ok"
    release_disconnect.set()


async def test_external_cancellation_releases_lock_when_operation_hangs(
    monkeypatch,
) -> None:
    """HA metadata timeout releases the lock despite stuck BLE cleanup."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    operation_cancelled = asyncio.Event()
    release_operation = asyncio.Event()

    async def get_connected_client():
        return fake_client

    async def resist_cancellation(_client):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            operation_cancelled.set()
            await release_operation.wait()

    async def ok(_client):
        return "ok"

    monkeypatch.setattr(client, "_get_connected_client", get_connected_client)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            client._run_with_client(resist_cancellation),
            timeout=0.01,
        )

    await operation_cancelled.wait()
    assert await client._run_with_client(ok) == "ok"
    release_operation.set()


async def test_disconnect_is_bounded_when_cleanup_hangs(monkeypatch) -> None:
    """Explicit disconnect returns after the cleanup deadline."""
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeWriteOnlyBleakClient()
    release_disconnect = asyncio.Event()

    async def disconnect():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            await release_disconnect.wait()

    fake_client.disconnect = disconnect
    client._client = fake_client
    monkeypatch.setattr("solem_blip_ble.client.DISCONNECT_TIMEOUT", 0.01)

    await asyncio.wait_for(client.disconnect(), timeout=0.05)

    assert client._client is None
    release_disconnect.set()


async def test_raw_ble_session_resolves_characteristics_and_disconnects() -> None:
    client = SolemClient("AA:BB:CC:DD:EE:FF")
    fake_client = FakeRawBleakClient()
    client._client = fake_client

    async with client.raw_ble_session() as session:
        assert session.client is fake_client
        assert session.notify_characteristic is fake_client.services.notify
        assert session.write_characteristic is fake_client.services.write

    assert client._client is None
    assert fake_client.disconnects == 1


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
