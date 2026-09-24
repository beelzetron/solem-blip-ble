"""Verified set-time transaction (BLE issue #50).

The set-time exchange is a *transaction*, not a write-only primitive:

1. read status first and defer when the controller is busy
   (``is_watering`` or ``active_program`` set);
2. write the set-time frame (no commit suffix) and wait for the 0x04-prefixed
   reply (``True`` = ack, ``False`` = explicit rejection);
3. re-read status and fail unless the time alarm bit has cleared — sync is
   only reported done when verified;
4. the whole operation runs on one non-retryable connection
   (``retry_safe=False``).

The writes sequence asserted everywhere: ``[set_time_payload, commit]`` —
the set-time frame never carries a commit suffix, and the commit frame is
used only for the status reads.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from solem_blip_ble import protocol
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import (
    SolemConnectionError,
    SolemTimeSyncBusy,
    SolemTimeSyncRejected,
    SolemTimeSyncVerificationFailed,
)
from test_client_v2 import FakeV2Client, established  # noqa: F401

SET_TIME_PAYLOAD = protocol.pack_set_time(datetime(2026, 9, 24, 12, 0, 0))
COMMIT = bytes.fromhex("3b00")

# 18-byte idle status frame: seq=2, status=0x40 (on, not watering, no alarm)
IDLE_STATUS = bytearray(18)
IDLE_STATUS[2] = 0x02
IDLE_STATUS[3] = 0x40

# Same idle frame with the time alarm bit set (0x40 | 0x20)
ALARM_STATUS = bytearray(18)
ALARM_STATUS[2] = 0x02
ALARM_STATUS[3] = 0x60

# 18-byte manual-watering frame: status=0x42 (on + activity bits)
WATERING_STATUS = bytearray(18)
WATERING_STATUS[2] = 0x02
WATERING_STATUS[3] = 0x42
WATERING_STATUS[9] = 1  # active station 1


def _frame(hex_str: str) -> bytearray:
    return bytearray.fromhex(hex_str)


class SetTimeScriptedClient(FakeV2Client):
    """Fake that answers each commit with a scripted status frame and can
    emit extra notifications after the set-time write."""

    def __init__(
        self,
        status_frames: list[bytes],
        post_write_replies: list[bytes] | None = None,
    ) -> None:
        super().__init__()
        self._status_frames = list(status_frames)
        self._post_write_replies = post_write_replies or []

    async def write_gatt_char(
        self, _uuid: str, payload: bytes, *, response: bool
    ) -> None:
        self.writes.append(payload)
        if payload == COMMIT:
            frame = self._status_frames.pop(0)
            self.handler(1, bytearray(frame))
            return
        if payload[:3] == SET_TIME_PAYLOAD[:3] and self._post_write_replies:
            for reply in self._post_write_replies:
                self.handler(1, bytearray(reply))
            return


def _client(monkeypatch, fake: FakeV2Client) -> StatelessSolemClient:
    async def fake_resolve(self):
        return object()

    async def fake_connect(self):
        return fake

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", fake_connect)
    monkeypatch.setattr(
        "solem_blip_ble.client_v2.NOTIFY_SETTLE_DELAY", 0
    )
    return StatelessSolemClient("AA:BB:CC:DD:EE:FF")


async def test_set_time_happy_path_writes_then_verifies(monkeypatch) -> None:
    """Ack reply + alarm-clear verification: writes are set-time (no commit)
    followed by exactly one commit for the verification status read."""
    ack = _frame("0400")
    fake = SetTimeScriptedClient(
        status_frames=[bytes(IDLE_STATUS), bytes(IDLE_STATUS)],
        post_write_replies=[bytes(ack)],
    )
    client = _client(monkeypatch, fake)

    await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT, SET_TIME_PAYLOAD, COMMIT]


async def test_set_time_happy_path_default_now(monkeypatch) -> None:
    """Without an explicit ``when`` the default payload is still verified."""
    ack = _frame("0400")
    fake = SetTimeScriptedClient(
        status_frames=[bytes(IDLE_STATUS), bytes(IDLE_STATUS)],
        post_write_replies=[bytes(ack)],
    )
    client = _client(monkeypatch, fake)

    await client.set_time()

    assert len(fake.writes) == 3
    assert fake.writes[0] == COMMIT
    assert fake.writes[1][:3] == SET_TIME_PAYLOAD[:3]
    assert fake.writes[2] == COMMIT


async def test_set_time_deferred_while_watering(monkeypatch) -> None:
    """A busy controller defers the sync: no set-time write is issued."""
    fake = SetTimeScriptedClient(status_frames=[bytes(WATERING_STATUS)])
    client = _client(monkeypatch, fake)

    with pytest.raises(SolemTimeSyncBusy, match="watering"):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT]


async def test_set_time_deferred_while_program_active(monkeypatch) -> None:
    """An active program defers the sync: no set-time write is issued."""
    program_status = bytearray(18)
    program_status[2] = 0x02
    program_status[3] = 0x40  # on, idle but...
    program_status[8] = 1  # ...program A active
    fake = SetTimeScriptedClient(status_frames=[bytes(program_status)])
    client = _client(monkeypatch, fake)

    with pytest.raises(SolemTimeSyncBusy, match="active program"):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT]


async def test_set_time_rejected_by_controller(monkeypatch) -> None:
    """An explicit 0xF0 rejection raises the rejection error and the
    verification status re-read is never started (single write)."""
    rejection = _frame("0402f014")
    fake = SetTimeScriptedClient(
        status_frames=[bytes(IDLE_STATUS)],
        post_write_replies=[bytes(rejection)],
    )
    client = _client(monkeypatch, fake)

    with pytest.raises(SolemTimeSyncRejected, match="rejected"):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT, SET_TIME_PAYLOAD]


async def test_set_time_verification_failure_time_alarm_still_set(
    monkeypatch,
) -> None:
    """An acknowledged set-time whose verification status still shows the
    alarm bit fails: the sync is only done when verified."""
    ack = _frame("0400")
    fake = SetTimeScriptedClient(
        status_frames=[bytes(IDLE_STATUS), bytes(ALARM_STATUS)],
        post_write_replies=[bytes(ack)],
    )
    client = _client(monkeypatch, fake)

    with pytest.raises(SolemTimeSyncVerificationFailed, match="time alarm"):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT, SET_TIME_PAYLOAD, COMMIT]


async def test_set_time_reply_timeout(monkeypatch) -> None:
    """No 0x04 reply within the window: standard timeout wording."""
    fake = SetTimeScriptedClient(status_frames=[bytes(IDLE_STATUS)])
    client = _client(monkeypatch, fake)
    monkeypatch.setattr(
        "solem_blip_ble.client_v2.STATUS_NOTIFY_TIMEOUT", 0.1
    )

    with pytest.raises(
        SolemConnectionError, match="Timeout waiting for set-time reply"
    ):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT, SET_TIME_PAYLOAD]


async def test_set_time_ignores_non_reply_notifications(monkeypatch) -> None:
    """Status and command notifications arriving between the write and the
    reply are ignored — only a 0x04-prefixed frame completes the wait."""
    ack = _frame("0400")
    noise = [
        bytes(IDLE_STATUS),  # seq=0x02 status frame
        _frame("3210024200aaaaaa00014f0c10003c100000"),  # command ack frame
        bytes(ack),
    ]
    fake = SetTimeScriptedClient(
        status_frames=[bytes(IDLE_STATUS), bytes(IDLE_STATUS)],
        post_write_replies=noise,
    )
    client = _client(monkeypatch, fake)

    await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert fake.writes == [COMMIT, SET_TIME_PAYLOAD, COMMIT]


async def test_set_time_is_not_retried(monkeypatch) -> None:
    """The transaction is non-retryable: a mid-exchange transport failure
    surfaces once instead of replaying the set-time write."""
    attempts = 0

    class DropOnSetTime(SetTimeScriptedClient):
        async def write_gatt_char(
            self, _uuid: str, payload: bytes, *, response: bool
        ) -> None:
            nonlocal attempts
            if payload[:3] == SET_TIME_PAYLOAD[:3]:
                attempts += 1
                raise SolemConnectionError("link dropped mid-transaction")
            await super().write_gatt_char(_uuid, payload, response=response)

    fake = DropOnSetTime(status_frames=[bytes(IDLE_STATUS)])
    client = _client(monkeypatch, fake)
    monkeypatch.setattr(
        "solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0
    )

    with pytest.raises(SolemConnectionError, match="link dropped"):
        await client.set_time(datetime(2026, 9, 24, 12, 0, 0))

    assert attempts == 1
    assert fake.writes == [COMMIT]


async def test_set_time_mock_short_circuit() -> None:
    """Mock mode returns immediately without any BLE operation."""
    client = StatelessSolemClient("AA:BB:CC:DD:EE:FF", mock=True)

    assert await client.set_time() is None
