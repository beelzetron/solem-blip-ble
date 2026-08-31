"""Tests for the v2 stateless connect-per-operation client."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from solem_blip_ble.client_v2 import StatelessSolemClient, _DropDetected
from solem_blip_ble.exceptions import SolemConnectionError, SolemDeadlineExceeded
from unittest.mock import MagicMock


class FakeV2Client:
    """Minimal fake of the bleak client surface used by v2 operations."""

    is_connected = True

    def __init__(self) -> None:
        self.handler = None
        self.writes: list[bytes] = []
        self.disconnects = 0
        self.services: list[Any] = []

    async def start_notify(self, _uuid: str, handler) -> None:
        self.handler = handler

    async def stop_notify(self, _uuid: str) -> None:
        self.handler = None

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        assert response is False
        if self.handler is not None and payload == bytes.fromhex("3b00"):
            self.handler(
                1,
                bytearray.fromhex("3210024200aaaaaa00014f0c10003c100000"),
            )

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


@pytest.fixture
def established(monkeypatch):
    """Patch resolution/connection so each _run_operation uses a fresh fake."""
    created: list[FakeV2Client] = []

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        fake = FakeV2Client()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    return created


async def test_status_roundtrip_single_connect(established) -> None:
    """A status poll connects exactly once, runs, and disconnects."""
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    monkey_delay = pytest.MonkeyPatch()
    monkey_delay.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    status = await client.get_status()
    monkey_delay.undo()

    assert status["is_watering"] is True
    assert status["station_num"] == 1
    assert len(established) == 1
    assert established[0].disconnects == 1
    assert bytes.fromhex("3b00") in established[0].writes


async def test_no_state_between_operations(established) -> None:
    """Two operations use two independent clients — nothing is reused."""
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    pytest.MonkeyPatch().setattr(
        "solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0
    )
    await client.get_status()
    await client.get_status()

    assert len(established) == 2
    assert established[0] is not established[1]
    assert established[0].disconnects == 1
    assert established[1].disconnects == 1


async def test_deadline_bounds_repeated_connect_hangs(monkeypatch) -> None:
    """A hanging connect loop is bounded by the deadline, non-retryable."""
    connect_calls = 0

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(100)
        raise AssertionError("should be cancelled before returning")  # pragma: no cover

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 0.3)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded, match="deadline"):
        await client.get_status()

    assert connect_calls <= 2


async def test_transient_error_retries_then_succeeds(monkeypatch) -> None:
    """A transient backend error retries the whole operation and recovers."""
    attempts = 0

    class FlakyClient(FakeV2Client):
        async def write_gatt_char(
            self, _uuid: str, payload: bytes, *, response: bool
        ) -> None:
            nonlocal attempts
            if payload == bytes.fromhex("3b00"):
                attempts += 1
            if attempts == 1 and payload == bytes.fromhex("3b00"):
                raise BleakError("transient radio hiccup")
            await super().write_gatt_char(_uuid, payload, response=response)

    async def fake_resolve(self):
        return object()

    connect_attempts = 0

    async def fake_connect(self):
        nonlocal connect_attempts
        connect_attempts += 1
        return FlakyClient()

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 10.0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    status = await client.get_status()

    assert status["is_watering"] is True
    assert connect_attempts == 2


async def test_persistent_failure_raises_deadline(monkeypatch) -> None:
    """Persistent SolemConnectionError exhausts the flat retry and raises."""
    connect_attempts = 0

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        nonlocal connect_attempts
        connect_attempts += 1
        raise SolemConnectionError("Failed connecting to device")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 10.0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded):
        await client.get_status()

    assert connect_attempts == 3  # REQUEST_MAX_ATTEMPTS, flat loop only


async def test_link_drop_fails_operation_immediately(monkeypatch) -> None:
    """A mid-operation drop raises SolemConnectionError right away."""
    state: dict[str, Any] = {}

    class DropThenHangClient(FakeV2Client):
        async def start_notify(self, _uuid: str, handler) -> None:
            # Backend fires the disconnect callback on the REAL client while
            # start_notify is in flight, then the operation hangs on the
            # dead link. The client itself confirms the drop via
            # is_connected=False (the confirmation _check_drop requires).
            self.is_connected = False
            state["real"]._link_dropped = True
            state["real"]._drop_event.set()
            await asyncio.sleep(100)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        state["fake"] = DropThenHangClient()
        return state["fake"]

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 10.0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    state["real"] = client
    with pytest.raises(SolemConnectionError, match="link dropped"):
        await asyncio.wait_for(client.get_status(), timeout=2.0)

    assert state["fake"].disconnects == 1


async def test_drop_hint_requires_client_confirmation() -> None:
    """A bare disconnect callback (hint) must not fail a healthy operation.

    Backends deliver the callback through wrapper objects whose identity is
    not connection-unique, and establish_connection reuses one callback
    across its internal attempts. The hint therefore only fails the
    operation when the active client confirms it is actually disconnected;
    a hint with a still-connected client is stale and gets cleared.
    """
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    active = MagicMock()
    active.is_connected = True
    client._active_client = active

    client._on_disconnected(object())  # type: ignore[arg-type]
    assert client._link_dropped is True
    assert client._drop_event.is_set()

    # Confirmation fails (client still connected): hint is cleared, no raise.
    client._check_drop()
    assert client._link_dropped is False
    assert not client._drop_event.is_set()

    # Confirmation succeeds (client disconnected): the drop is fatal.
    active.is_connected = False
    client._on_disconnected(object())  # type: ignore[arg-type]
    with pytest.raises(_DropDetected):
        client._check_drop()


async def test_teardown_hint_does_not_poison_next_operation(
    monkeypatch,
) -> None:
    """A disconnect callback firing during intentional teardown must not
    poison the next operation — the live-hardware failure chain (op N's
    teardown hint killing op N+1) must stay impossible."""
    state: dict[str, Any] = {}

    class HintDuringTeardownClient(FakeV2Client):
        async def disconnect(self) -> None:
            self.disconnects += 1
            self.is_connected = False
            # Backend delivers the disconnect callback while the client is
            # being torn down. Under confirmation semantics this leaves a
            # hint behind, but the next operation must succeed anyway:
            # _connect clears it and _check_drop only raises with
            # client-confirmed disconnection.
            state["real"]._on_disconnected(self)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        c = HintDuringTeardownClient()
        state.setdefault("clients", []).append(c)
        return c

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 10.0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    state["real"] = client
    await asyncio.wait_for(client.get_status(), timeout=5.0)
    assert client._link_dropped is True  # hint left by teardown...

    # ...but the next operation is unaffected (the old bug killed this one).
    await asyncio.wait_for(client.get_status(), timeout=5.0)
    first, second = state["clients"]
    assert first.disconnects == 1
    assert second.disconnects == 1


async def test_ble_device_cache_expires(monkeypatch) -> None:
    """Resolve is skipped while the cache is fresh, and re-run after expiry."""
    fake_connect = asyncio.Event()

    async def fake_connect_impl(self):
        return FakeV2Client()

    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect_impl)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    fresh_device = object()  # type: ignore[assignment]
    client._ble_device = fresh_device
    client._ble_device_cached_at = time.monotonic()

    resolves: list[BLEDevice | None] = []

    def counting_resolver() -> BLEDevice | None:
        resolves.append(None)
        return None  # resolver yields nothing -> connect fails

    client._ble_device_resolver = counting_resolver  # type: ignore[assignment]

    # Fresh cache: the resolver is not consulted at connect time.
    await client._connect()
    assert len(resolves) == 0
    assert client._ble_device is fresh_device

    # Expired cache: re-resolve consults the resolver; with a resolver that
    # yields nothing, resolution fails.
    client._ble_device_cached_at = time.monotonic() - 60.0
    with pytest.raises(SolemConnectionError):
        await client._resolve_ble_device()
    assert len(resolves) == 1

    # Resolver returning a device: re-resolve succeeds and refreshes the cache.
    fresh2 = object()

    def resolver_with_device() -> BLEDevice | None:
        resolves.append(None)
        return fresh2  # type: ignore[return-value]

    client._ble_device_resolver = resolver_with_device  # type: ignore[assignment]
    device = await client._resolve_ble_device()
    assert device is fresh2
    assert len(resolves) == 2
    assert client._ble_device is fresh2


async def test_mock_mode_stays_off_ble(monkeypatch) -> None:
    """Mock mode returns protocol data without touching connection code."""

    async def fail_connect(self):
        raise AssertionError("mock mode must not connect")

    monkeypatch.setattr(StatelessSolemClient, "_connect", fail_connect)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", mock=True)
    status = await client.get_status()
    assert status["is_watering"] is False
    assert await client.get_firmware_version() == {
        "major": 5,
        "minor": 0,
        "patch": 0,
        "raw_hex": "5.0.0",
    }
