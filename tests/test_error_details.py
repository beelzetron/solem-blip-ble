"""Underlying error details propagate into wrapper messages (#51).

Bare ``asyncio.TimeoutError`` (empty ``str()``) and opaque backend errors
made the ``Attempt N failed:`` log line and the final
``SolemDeadlineExceeded`` indistinguishable from other failures. The
connect wrappers now embed the underlying exception text, and the
deadline-exhaustion path appends ``last_error``.
"""

from __future__ import annotations

import asyncio
import logging as _logging
from typing import Any

import pytest

from solem_blip_ble.client_persistent import PersistentSolemClient
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import SolemConnectionError, SolemDeadlineExceeded

_MAC = "AA:BB:CC:DD:EE:FF"


async def test_connect_wrapper_includes_backend_error_text(monkeypatch) -> None:
    """The stateless connect wrapper embeds the underlying BleakError text."""
    async def fake_resolve(self):
        return object()

    async def failing_connect(self):
        raise SolemConnectionError("Failed connecting to device: adapter powered off")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", failing_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 5.0)

    client = StatelessSolemClient(_MAC)
    with pytest.raises(SolemDeadlineExceeded, match="adapter powered off"):
        await client.get_status()


async def test_connect_wrapper_keeps_message_prefix(monkeypatch) -> None:
    """The wrapper keeps its recognizable prefix; only details are appended."""
    async def fake_resolve(self):
        return object()

    async def failing_connect(self):
        raise SolemConnectionError("Failed connecting to device: boom")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", failing_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 5.0)

    client = StatelessSolemClient(_MAC)
    with pytest.raises(
        SolemDeadlineExceeded, match=r"Failed connecting to device: boom$"
    ):
        await client.get_status()


async def test_deadline_message_carries_last_error(monkeypatch) -> None:
    """Deadline exhaustion appends the last attempt's error to the message."""
    attempts = 0

    async def failing_connect(self):
        nonlocal attempts
        attempts += 1
        raise SolemConnectionError("Failed connecting to device: link timeout")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", _fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", failing_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.REQUEST_RETRY_DELAY", 0)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 0.2)

    client = StatelessSolemClient(_MAC)
    with pytest.raises(SolemDeadlineExceeded) as excinfo:
        await client.get_status()

    message = str(excinfo.value)
    assert "deadline exceeded" in message
    assert "Failed connecting to device: link timeout" in message
    assert attempts >= 1


async def test_deadline_message_without_last_error_has_no_trailer(
    monkeypatch,
) -> None:
    """A deadline hit before any attempt leaves the message trailer-free."""
    async def hanging_connect(self):
        await asyncio.sleep(100)  # cancelled when the deadline expires

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", _fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", hanging_connect)
    monkeypatch.setattr("solem_blip_ble.client_v2.OPERATION_DEADLINE", 0.2)

    client = StatelessSolemClient(_MAC)
    with pytest.raises(SolemDeadlineExceeded, match="Connect phase timed out after"):
        await client.get_status()


async def test_persistent_deadline_message_carries_last_error(monkeypatch) -> None:
    """The persistent executor's deadline message embeds the last error too."""
    async def failing_connect(self):
        raise SolemConnectionError("Failed connecting to device: powered off")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", _fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", failing_connect)
    monkeypatch.setattr(
        "solem_blip_ble.client_persistent.REQUEST_RETRY_DELAY", 0
    )
    monkeypatch.setattr(
        "solem_blip_ble.client_persistent.OPERATION_DEADLINE", 0.2
    )

    client = PersistentSolemClient(_MAC)
    with pytest.raises(
        SolemDeadlineExceeded, match=r"deferring retry to the next operation: .*powered off$"
    ):
        await client.get_status()


async def test_persistent_connect_wrapper_includes_backend_error_text(
    monkeypatch,
) -> None:
    """The persistent path reuses the stateless wrappers, so details flow."""
    async def fake_resolve(self):
        return object()

    async def failing_connect(self):
        raise SolemConnectionError("Failed connecting to device: proxy unreachable")

    monkeypatch.setattr(StatelessSolemClient, "_resolve_ble_device", fake_resolve)
    monkeypatch.setattr(StatelessSolemClient, "_connect", failing_connect)
    monkeypatch.setattr(
        "solem_blip_ble.client_persistent.REQUEST_RETRY_DELAY", 0
    )
    monkeypatch.setattr(
        "solem_blip_ble.client_persistent.OPERATION_DEADLINE", 5.0
    )

    client = PersistentSolemClient(_MAC)
    with pytest.raises(
        SolemDeadlineExceeded, match=r"proxy unreachable$"
    ):
        await client.get_status()


async def _fake_resolve(self: Any):
    return object()
