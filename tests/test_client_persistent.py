"""Tests for the persistent-connection client (v2 line)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache

from solem_blip_ble.client_persistent import PersistentSolemClient
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import SolemConnectionError, SolemDeadlineExceeded


async def drain_background_disconnects() -> None:
    """Let detached teardown tasks run to completion before asserting."""
    for _ in range(10):
        await asyncio.sleep(0.01)


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
    """Patch resolution/connection so each connect returns a fresh fake."""
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


@pytest.fixture
def no_settle(monkeypatch):
    """Trim protocol settle delays so tests run fast."""
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)


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


async def test_connect_phase_timeout_single_attempt_then_next_operation_reconnects(
    monkeypatch,
) -> None:
    """Connect-phase timeout: the persistent client makes exactly one
    connect attempt, raises immediately (no same-operation retry), and the
    next operation reconnects."""
    connect_calls = 0
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        # Short-circuit real waiting (e.g. the 8 s retry spacing) while
        # recording what delay the code asked for.
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)

    created: list[FakeV2Client] = []

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            # As if establish_connection timed out waiting for the device.
            raise asyncio.TimeoutError()
        fake = FakeV2Client()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded, match="Connect phase"):
        await client.get_status()

    # Exactly one connect attempt: no same-operation retry.
    assert connect_calls == 1
    # No retry spacing was applied (give-up was immediate).
    assert 8.0 not in sleeps
    # Session invalidated.
    assert client._active_client is None
    assert client._link_dropped is False
    assert not client._drop_event.is_set()

    # The next operation reconnects and succeeds.
    status = await client.get_status()
    assert status["is_watering"] is True
    assert connect_calls == 2
    assert client._active_client is created[0]
    await drain_background_disconnects()
    assert created[0].disconnects == 0


async def test_connect_phase_connection_error_single_attempt(monkeypatch) -> None:
    """The 'Failed connecting to device' wrapper is also a connect-phase
    failure: single attempt, immediate deadline-style failure."""
    connect_calls = 0

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        nonlocal connect_calls
        connect_calls += 1
        raise SolemConnectionError("Failed connecting to device")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded, match="Connect phase"):
        await client.get_status()

    assert connect_calls == 1
    assert client._active_client is None


async def test_operation_phase_failure_retries_with_eight_second_spacing(
    monkeypatch,
) -> None:
    """A failure after the connect succeeded (write failure on a live link)
    keeps the flat retry, with REQUEST_RETRY_DELAY (8.0) spacing between
    attempts."""
    created: list[FakeV2Client] = []
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        # Short-circuit real waiting while recording the requested delay.
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_persistent.OPERATION_DEADLINE", 20.0)

    class FailOnceWriteClient(FakeV2Client):
        async def write_gatt_char(
            self, _uuid: str, payload: bytes, *, response: bool
        ) -> None:
            if len(created) == 1 and payload == bytes.fromhex("3b00"):
                raise BleakError("link hiccup mid-operation")
            await super().write_gatt_char(_uuid, payload, response=response)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        fake = FailOnceWriteClient()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    status = await client.get_status()

    assert status["is_watering"] is True
    # The operation-phase failure was retried on a fresh session...
    assert len(created) == 2
    # ...spaced by REQUEST_RETRY_DELAY = 8.0.
    assert 8.0 in sleeps
    assert client._active_client is created[1]
    await drain_background_disconnects()
    assert created[0].disconnects == 1
    assert created[1].disconnects == 0


async def test_reuses_one_connection_across_operations(
    established, no_settle
) -> None:
    """Two consecutive get_status calls produce exactly ONE connect."""
    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    status = await client.get_status()
    assert status["is_watering"] is True
    assert status["station_num"] == 1

    status2 = await client.get_status()
    assert status2["is_watering"] is True

    assert len(established) == 1
    assert established[0].disconnects == 0
    assert established[0].handler is None  # notify stopped between ops
    assert established[0].writes.count(bytes.fromhex("3b00")) == 2


async def test_operation_failure_invalidates_session(monkeypatch) -> None:
    """A failed operation releases the link; the next one reconnects fresh."""
    created: list[FakeV2Client] = []

    class FailingWriteClient(FakeV2Client):
        async def write_gatt_char(
            self, _uuid: str, payload: bytes, *, response: bool
        ) -> None:
            if payload == bytes.fromhex("3b00"):
                raise BleakError("radio hiccup")
            self.writes.append(payload)

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        # First client fails writes; any later client is healthy.
        fake = (
            FailingWriteClient()
            if not created
            else FakeV2Client()
        )
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 10.0)
    # Single attempt: the first operation fails without retry reconnects,
    # so the next operation is the only reconnect (2 connects total).
    monkeypatch.setattr("solem_blip_ble.client_persistent.REQUEST_MAX_ATTEMPTS", 1)

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded):
        await client.get_status()

    # Failure path: session fully invalidated.
    assert client._active_client is None
    assert client._link_dropped is False
    assert not client._drop_event.is_set()
    await drain_background_disconnects()
    assert all(c.disconnects == 1 for c in created)

    # Next operation reconnects on a clean session and succeeds.
    status = await client.get_status()
    assert status["is_watering"] is True
    assert client._active_client is created[-1]
    assert created[-1].disconnects == 0


async def test_confirmed_drop_kills_attempt_then_recovers(monkeypatch) -> None:
    """A confirmed mid-operation drop aborts the attempt, invalidates the
    held session, and the flat retry reconnects fresh."""
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

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")

    start = time.monotonic()
    status = await asyncio.wait_for(client.get_status(), timeout=15.0)
    elapsed = time.monotonic() - start

    assert status["is_watering"] is True
    clients = state["clients"]
    assert len(clients) == 2
    # The dropped first client was torn down in the background; the held
    # (second) client stays connected for reuse.
    assert clients[0].disconnects == 1
    assert clients[1].disconnects == 0
    assert client._active_client is clients[1]
    assert elapsed < 20.0


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
    monkeypatch.setattr("solem_blip_ble.client_persistent.OPERATION_DEADLINE", 0.3)
    monkeypatch.setattr(
        "solem_blip_ble.client_persistent.REQUEST_RETRY_DELAY", 0
    )

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(SolemDeadlineExceeded, match="deadline"):
        await client.get_status()

    assert connect_calls <= 2
    # Session is clean after the deadline failure.
    assert client._active_client is None
    assert client._link_dropped is False


async def test_disconnect_tears_down_and_next_operation_reconnects(
    established, no_settle
) -> None:
    """disconnect() with no in-flight work closes the link; the next
    operation establishes a new one."""
    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    await client.get_status()
    assert len(established) == 1

    await client.disconnect()
    await drain_background_disconnects()
    assert client._active_client is None
    assert established[0].disconnects == 1

    # Safe to call again when already disconnected.
    await client.disconnect()

    await client.get_status()
    assert len(established) == 2
    assert established[1].disconnects == 0
    assert client._active_client is established[1]


async def test_disconnect_waits_for_in_flight_operation(
    monkeypatch, no_settle
) -> None:
    """disconnect() acquires the session lock: it cannot sever an in-flight
    operation — the operation completes and returns its result first."""
    created: list[FakeV2Client] = []
    op_done = asyncio.Event()

    class SlowCommitClient(FakeV2Client):
        async def write_gatt_char(
            self, uuid: str, payload: bytes, *, response: bool
        ) -> None:
            await super().write_gatt_char(uuid, payload, response=response)
            if payload == bytes.fromhex("3b00"):
                # Keep the operation in flight briefly so disconnect()
                # must queue behind the session lock.
                await asyncio.sleep(0.2)
                op_done.set()

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        fake = SlowCommitClient()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)

    client = PersistentSolemClient("AA:BB:CC:DD:EE:FF")
    op_task = asyncio.create_task(client.get_status())
    # Give the operation time to acquire the lock and start writing.
    await asyncio.sleep(0.05)
    disconnect_task = asyncio.create_task(client.disconnect())

    status = await asyncio.wait_for(op_task, timeout=5.0)
    assert status["is_watering"] is True
    assert op_done.is_set()

    # Teardown only happens after the operation completed.
    await asyncio.wait_for(disconnect_task, timeout=5.0)
    await drain_background_disconnects()
    assert len(created) == 1
    assert created[0].disconnects == 1
    assert client._active_client is None



async def test_idle_release_and_reschedule(monkeypatch) -> None:
    """idle_release_seconds releases the link after the idle window; frequent
    activity keeps the link alive (timer rescheduled per operation)."""
    created: list[FakeV2Client] = []

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        fake = FakeV2Client()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)

    client = PersistentSolemClient(
        "AA:BB:CC:DD:EE:FF", idle_release_seconds=0.3
    )
    await client.get_status()
    assert len(created) == 1

    # Activity inside the idle window reschedules the timer: no release.
    await asyncio.sleep(0.15)
    await client.get_status()
    assert created[0].disconnects == 0

    # Quiet past the window: the link is released.
    await asyncio.sleep(0.5)
    assert created[0].disconnects == 1
    assert client._active_client is None
    assert client._link_dropped is False
    assert not client._drop_event.is_set()

    # Next operation reconnects fresh.
    status = await client.get_status()
    assert status["is_watering"] is True
    assert len(created) == 2
    assert created[1].disconnects == 0


async def test_no_stale_client_reuse_after_release(monkeypatch) -> None:
    """After a release (idle or explicit) the session state is reset
    synchronously, so a stale cached client can never be reused (#31)."""
    created: list[FakeV2Client] = []

    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        fake = FakeV2Client()
        created.append(fake)
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0)

    client = PersistentSolemClient(
        "AA:BB:CC:DD:EE:FF", idle_release_seconds=0.1
    )
    await client.get_status()
    held = created[0]

    # Capture the generation while the client is held.
    generation_while_held = client._session_generation

    await asyncio.sleep(0.3)  # past the idle window
    assert held.disconnects == 1
    # State was already reset by the time the teardown completed: the next
    # operation cannot touch the stale client.
    assert client._active_client is None
    assert client._session_generation > generation_while_held

    await client.get_status()
    assert len(created) == 2
    assert created[1] is not held
    assert client._active_client is created[1]
