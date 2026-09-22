"""Tests for the v2 stateless connect-per-operation client."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from solem_blip_ble import protocol
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from solem_blip_ble.client_v2 import (
    StatelessSolemClient,
    _ConnectTimedOut,
    _DropDetected,
)
from bleak_retry_connector import BleakClientWithServiceCache
from solem_blip_ble.exceptions import SolemConnectionError, SolemDeadlineExceeded
from unittest.mock import AsyncMock, MagicMock


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


async def test_connect_passes_single_attempt_to_establish_connection(
    monkeypatch,
) -> None:
    """establish_connection is called with max_attempts=1.

    The controller is a single-connection device that stops advertising
    during and for tens of seconds after a connect attempt, so an
    immediate internal retry (bleak-retry-connector's default double-tap)
    is guaranteed to fail and burns the operation deadline. Retries are
    provided by the operation-level flat loop, spaced by
    REQUEST_RETRY_DELAY.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_establish_connection(client_class, ble_device, **kwargs):
        calls.append(((client_class, ble_device), kwargs))
        return FakeV2Client()

    monkeypatch.setattr(
        "solem_blip_ble.client_v2.establish_connection",
        fake_establish_connection,
    )

    async def fake_resolve(self):
        return object()

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    fake = await client._connect()

    assert isinstance(fake, FakeV2Client)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] is BleakClientWithServiceCache
    assert kwargs["max_attempts"] == 1


def test_request_retry_delay_is_eight_seconds() -> None:
    """The operation-level retry spacing is 8 s: on a single-connection
    controller an immediate retry is guaranteed to fail while the device
    is still not advertising; isolated spaced attempts succeed."""
    from solem_blip_ble import client_v2, const

    assert const.REQUEST_RETRY_DELAY == 8.0
    assert client_v2.REQUEST_RETRY_DELAY == 8.0


async def test_retry_attempts_are_spaced_by_request_retry_delay(
    monkeypatch,
) -> None:
    """Between flat-retry attempts the executor waits REQUEST_RETRY_DELAY
    (8.0 s) — with the deadline still bounding the whole operation."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        # Short-circuit real waiting while recording the requested delay.
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 30.0)

    attempts = 0

    class FlakyClient(FakeV2Client):
        async def write_gatt_char(
            self, _uuid: str, payload: bytes, *, response: bool
        ) -> None:
            nonlocal attempts
            if payload == bytes.fromhex("3b00"):
                attempts += 1
                if attempts == 1:
                    raise BleakError("transient radio hiccup")
            await super().write_gatt_char(_uuid, payload, response=response)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        return FlakyClient()

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    status = await client.get_status()

    assert status["is_watering"] is True
    assert attempts == 2
    assert sleeps.count(8.0) == 1


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


async def test_connect_phase_timeout_carries_reason_in_log_and_chain(
    monkeypatch, caplog
) -> None:
    """A connect-phase wait_for timeout logs a NON-empty reason.

    asyncio.wait_for raises a bare TimeoutError whose str() is empty, which
    made the 'Attempt N failed:' debug line useless for triage (the live
    5-day outage: 'Attempt 1 failed: ' with no reason). The connect-phase
    timeout is now re-raised as _ConnectTimedOut carrying the phase, the
    budget, and the likely causes.
    """
    import logging as _logging

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        await asyncio.sleep(100)
        raise AssertionError("should be cancelled before returning")  # pragma: no cover

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 0.3)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    with caplog.at_level(_logging.DEBUG, logger="solem_blip_ble.client_v2"):
        with pytest.raises(SolemDeadlineExceeded) as excinfo:
            await client.get_status()

    cause = excinfo.value.__cause__
    assert isinstance(cause, _ConnectTimedOut)
    # It is still an asyncio.TimeoutError: existing except tuples match.
    assert isinstance(cause, asyncio.TimeoutError)
    assert "Connect phase timed out after" in str(cause)
    assert "refusing connections" in str(cause)
    assert cause.args[0].endswith("connections)")

    failure_lines = [
        rec.getMessage()
        for rec in caplog.records
        if "Attempt 1 failed:" in rec.getMessage()
    ]
    assert failure_lines, "the attempt-failure debug line must still be logged"
    for line in failure_lines:
        assert "Connect phase timed out after 0s" in line
        assert "refusing connections" in line
        assert line.rstrip().endswith("failed: ") is False or "refusing" in line


async def test_connect_timed_out_is_caught_by_same_except_tuple(monkeypatch) -> None:
    """_ConnectTimedOut raised mid-attempt is caught by the existing
    except tuple in _run_operation (it is an asyncio.TimeoutError
    subclass), so the flat retry still applies — proven via the
    retry count."""
    connect_calls = 0
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 60.0)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        nonlocal connect_calls
        connect_calls += 1
        raise _ConnectTimedOut(
            "Connect phase timed out after 23s (device not advertising, "
            "out of range, or refusing connections)"
        )

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded, match="3 attempt"):
        await client.get_status()

    # Caught and retried every time by the same except tuple as the bare
    # TimeoutError: REQUEST_MAX_ATTEMPTS (3) attempts, spaced 8 s apart.
    assert connect_calls == 3
    assert sleeps.count(8.0) == 2


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


async def test_confirmed_drop_kills_attempt_then_recovers(monkeypatch) -> None:
    """A confirmed mid-operation drop aborts the attempt immediately instead
    of hanging, then the flat retry reconnects fresh and recovers.

    Single-client devices can kill the new link when the previous
    connection has not fully released; with per-attempt fresh connects and
    the retry delay, a drop is recoverable within the deadline."""
    state: dict[str, Any] = {}

    class DropThenHangClient(FakeV2Client):
        async def start_notify(self, _uuid: str, handler) -> None:
            # Attempt 1: the backend confirms a real drop while start_notify
            # is in flight, then the operation would hang on the dead link.
            # Attempt 2 (fresh client): healthy subscribe.
            if state.get("dropped"):
                self.handler = handler
                return
            state["dropped"] = True
            self.is_connected = False
            state["real"]._link_dropped = True
            state["real"]._drop_event.set()
            await asyncio.sleep(100)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        state["real"] = self
        client = DropThenHangClient()
        state.setdefault("clients", []).append(client)
        return client

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 20.0)

    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF")

    start = time.monotonic()
    status = await asyncio.wait_for(client.get_status(), timeout=15.0)
    elapsed = time.monotonic() - start

    assert status["is_watering"] is True
    # Two fresh connects; the dropped first client was closed; the whole
    # operation stayed well inside the 20 s deadline.
    clients = state["clients"]
    assert len(clients) == 2
    assert clients[0].disconnects == 1
    assert elapsed < 20.0


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


async def test_write_irrigation_program_skips_readback(monkeypatch) -> None:
    """Write-only schedule frames use one minimal BLE operation per frame."""
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", max_station_num=2)
    program = {
        "name": "Programme B",
        "inter_station_delay": 0,
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 3,
        "synchro_day": 1,
        "period_start_date": None,
        "start_times": [360, None, None, None, None, None, None, None],
        "station_durations": [600, 600],
    }
    operation_writes: list[list[bytes]] = []

    async def run_operation(operation, *, deadline=None):
        fake = FakeV2Client()
        await operation(fake)
        operation_writes.append(fake.writes)
        assert fake.handler is None

    monkeypatch.setattr(client, "_run_operation", run_operation)

    await client.write_irrigation_program(1, program)

    frames = protocol.pack_set_irrigation_program(
        1,
        program,
        max_stations=2,
    )
    assert operation_writes == [[frame] for frame in frames]


async def test_set_irrigation_program_uses_write_only_primitive(monkeypatch) -> None:
    """Verified writes delegate the BLE write phase to the write-only primitive."""
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", mock=True, max_station_num=2)
    program = {
        "name": "Programme B",
        "inter_station_delay": 0,
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 3,
        "synchro_day": 1,
        "period_start_date": None,
        "start_times": [360, None, None, None, None, None, None, None],
        "station_durations": [600, 600],
    }
    expected = protocol.normalize_irrigation_program_for_write(
        program,
        max_stations=2,
    )
    write = AsyncMock()
    readback = AsyncMock(return_value={1: expected})
    monkeypatch.setattr(client, "write_irrigation_program", write)
    monkeypatch.setattr(client, "get_irrigation_config", readback)
    client.mock = False

    result = await client.set_irrigation_program(1, program)

    write.assert_awaited_once_with(1, program)
    readback.assert_awaited_once()
    assert result == {1: expected}
