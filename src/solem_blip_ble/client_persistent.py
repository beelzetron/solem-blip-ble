"""Persistent-connection BLE client for Solem BL-IP controllers.

Extends the v2 stateless client (:class:`StatelessSolemClient`) with a
**persistent session mode**: instead of a full BLE handshake per poll, one
connection is held across operations. This restores the connection behavior
of the 0.1.x ``SolemClient`` for hardware that is destabilized by frequent
connect churn — the BL-IP controller is a weak single-connection device and
its GATT layer can wedge under per-poll handshakes — while keeping every v2
guarantee:

- **Bounded worst case**: the same whole-operation deadline and flat retry
  loop as the stateless executor; a hanging connect is bounded by the
  remaining deadline.
- **Active disconnect detection**: the same disconnect-callback hint plus
  client-confirmed drop semantics (inherited unchanged).
- **Single-link serialization**: a session lock serializes operations so
  concurrent public calls cannot interleave on the shared link, and
  :meth:`disconnect` cannot sever an in-flight operation.
- **Synchronous invalidation before await** (0.1.x #31 pattern): when a
  stale or dropped client is found, session state is cleared *before* any
  await, so a resisting teardown can never poison the next operation; the
  physical disconnect itself runs as a bounded background task.
- **Optional idle release**: with ``idle_release_seconds`` set, the link is
  released after that much idle time, and the timer is rescheduled by
  activity.

Reuse: all operation closures are inherited from
:class:`StatelessSolemClient` unchanged — they pass their work through
``self._run_operation``; overriding only :meth:`_run_operation` (plus the
teardown/idle plumbing) turns the stateless executor into a persistent one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .client_v2 import (
    _BACKEND_ERRORS,
    _await_operation,
    _DropDetected,
    StatelessSolemClient,
)
from .const import (
    DEFAULT_BLUETOOTH_TIMEOUT,
    MAX_STATION_NUM,
    OPERATION_DEADLINE,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
)
from .exceptions import SolemConnectionError, SolemDeadlineExceeded

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Every teardown disconnect runs bounded in the background: a resisting
# disconnect must never block the next operation or the idle/disconnect
# path (0.1.x #31/#32 pattern).
RELEASE_DISCONNECT_TIMEOUT = 5.0


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Observe a detached background task exception after cancellation."""
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


class PersistentSolemClient(StatelessSolemClient):
    """Persistent-connection variant of the v2 stateless client.

    Holds one BLE connection across operations. The public API is identical
    to :class:`StatelessSolemClient` except that a new
    ``idle_release_seconds`` constructor parameter controls optional
    automatic link release, and an explicit ``await disconnect()`` closes
    the held link.
    """

    def __init__(
        self,
        mac_address: str,
        bluetooth_timeout: float = DEFAULT_BLUETOOTH_TIMEOUT,
        *,
        mock: bool = False,
        max_station_num: int = MAX_STATION_NUM,
        ble_device: BLEDevice | None = None,
        ble_device_resolver: Callable[[], BLEDevice | None] | None = None,
        idle_release_seconds: float | None = None,
    ) -> None:
        super().__init__(
            mac_address,
            bluetooth_timeout,
            mock=mock,
            max_station_num=max_station_num,
            ble_device=ble_device,
            ble_device_resolver=ble_device_resolver,
        )
        self.idle_release_seconds = idle_release_seconds
        self._session_lock = asyncio.Lock()
        self._session_generation = 0
        self._idle_task: asyncio.Task[None] | None = None

    # -- teardown / invalidation -------------------------------------------

    def _reset_session_state(self) -> None:
        """Clear all session state synchronously (no awaits).

        Must be called before any await so a cancellation or a resisting
        disconnect can never leave a stale client behind (0.1.x #31).
        """
        self._active_client = None
        self._link_dropped = False
        self._drop_event.clear()
        self._session_generation += 1

    def _schedule_background_disconnect(self, client: BleakClient) -> None:
        """Detach a bounded background disconnect for the given client."""

        async def _release() -> None:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=RELEASE_DISCONNECT_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "%s - Background disconnect of released client: %s",
                    self.mac_address,
                    exc,
                )

        task = asyncio.create_task(_release())
        task.add_done_callback(_consume_task_exception)

    # -- idle release --------------------------------------------------------

    def _cancel_idle_task(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _schedule_idle_release(self) -> None:
        """(Re)arm the idle-release timer, if configured.

        Called while the session lock is held; the idle task itself never
        acquires the session lock while awaiting the disconnect — it resets
        the session state synchronously and detaches the bounded teardown.
        """
        self._cancel_idle_task()
        if self.idle_release_seconds is None:
            return

        async def _idle_release() -> None:
            idle_release_seconds = self.idle_release_seconds
            if idle_release_seconds is None:  # pragma: no cover - re-check
                return
            try:
                await asyncio.sleep(idle_release_seconds)
            except asyncio.CancelledError:
                raise
            _LOGGER.debug(
                "%s - Idle timeout reached; releasing persistent link",
                self.mac_address,
            )
            # Reset state synchronously, then disconnect bounded in the
            # background without holding the session lock.
            client = self._active_client
            self._reset_session_state()
            if client is not None:
                self._schedule_background_disconnect(client)

        self._idle_task = asyncio.create_task(_idle_release())

    # -- persistent executor -------------------------------------------------

    async def _run_operation(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
        *,
        deadline: float | None = None,
    ) -> _T:
        """Run one operation over a held connection, keeping it on success.

        Same structure as the stateless executor: whole-operation deadline,
        flat retry loop, drop watcher racing the operation task. The only
        difference: the client is *not* disconnected on success — the link
        is reused by the next operation. On failure the session is
        invalidated (synchronously, then bounded background teardown) so
        the next attempt inside the same operation reconnects fresh.
        """
        if self.mock:
            raise SolemConnectionError("mock client has no BLE operations")

        self._cancel_idle_task()
        async with self._session_lock:
            if deadline is None:
                # Read at call time so monkeypatching the module constant works.
                deadline = OPERATION_DEADLINE
            deadline_at = time.monotonic() + deadline
            last_error: Exception | None = None

            for attempt in range(1, REQUEST_MAX_ATTEMPTS + 1):
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    self._reset_session_state()
                    self._schedule_idle_release()
                    raise SolemDeadlineExceeded(
                        f"Operation deadline exceeded after {attempt - 1} attempt(s)"
                    ) from last_error

                reused = (
                    self._active_client is not None
                    and self._active_client.is_connected
                    and not self._link_dropped
                )
                op_task: asyncio.Task[_T] | None = None
                drop_task: asyncio.Task[None] | None = None
                try:
                    if reused:
                        client = self._active_client
                        assert client is not None
                        _LOGGER.debug(
                            "%s - Reusing persistent connection",
                            self.mac_address,
                        )
                        # Clear a stale drop hint so it cannot fail this
                        # healthy operation (hint left by the previous
                        # teardown or a spurious callback).
                        self._link_dropped = False
                        self._drop_event.clear()
                    else:
                        if self._active_client is not None:
                            # Tear down the stale client first, resetting
                            # session state synchronously before awaiting
                            # anything (0.1.x #31 pattern).
                            stale = self._active_client
                            _LOGGER.debug(
                                "%s - Cached client stale; invalidating session",
                                self.mac_address,
                            )
                            self._reset_session_state()
                            self._schedule_background_disconnect(stale)
                        client = await asyncio.wait_for(
                            self._connect(), timeout=remaining
                        )
                        self._active_client = client
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        raise SolemDeadlineExceeded(
                            "Operation deadline exhausted during connect"
                        )
                    op_task = asyncio.create_task(
                        _await_operation(operation(client))
                    )
                    drop_task = asyncio.create_task(self._watch_drop(client))
                    done, _ = await asyncio.wait(
                        {op_task, drop_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if drop_task in done:
                        # Confirmed drop (client says it is disconnected):
                        # abort the attempt immediately instead of hanging
                        # on the dead link; the session is invalidated below
                        # so the flat retry reconnects fresh.
                        raise _DropDetected()
                    if op_task not in done:
                        raise asyncio.TimeoutError(
                            f"Operation deadline exceeded during attempt {attempt}"
                        )
                    result = op_task.result()
                    _LOGGER.debug(
                        "%s - Operation succeeded; holding persistent connection",
                        self.mac_address,
                    )
                    # Success: keep the client and arm the idle timer.
                    self._schedule_idle_release()
                    return result
                except asyncio.CancelledError:
                    raise
                except SolemDeadlineExceeded:
                    self._reset_session_state()
                    raise
                except (
                    asyncio.TimeoutError,
                    *_BACKEND_ERRORS,
                    SolemConnectionError,
                    _DropDetected,
                ) as exc:
                    last_error = exc
                    _LOGGER.debug(
                        "%s - Attempt %d failed: %s",
                        self.mac_address,
                        attempt,
                        exc,
                    )
                finally:
                    if op_task is not None and not op_task.done():
                        op_task.cancel()
                    if drop_task is not None:
                        drop_task.cancel()
                    # On success the client is kept (idle timer armed above);
                    # on any other exit the session is invalidated
                    # synchronously and the link torn down in the bounded
                    # background, so the next attempt or next operation
                    # starts fresh.
                    if op_task is None or not op_task.done():
                        op_failed = True
                    elif op_task.cancelled():
                        op_failed = True
                    else:
                        op_failed = op_task.exception() is not None
                    if op_failed:
                        client_to_release = self._active_client
                        if client_to_release is not None:
                            self._reset_session_state()
                            self._schedule_background_disconnect(client_to_release)

                if (
                    time.monotonic() < deadline_at
                    and attempt < REQUEST_MAX_ATTEMPTS
                ):
                    await asyncio.sleep(REQUEST_RETRY_DELAY)

            self._reset_session_state()
            raise SolemDeadlineExceeded(
                f"Operation deadline exceeded after {REQUEST_MAX_ATTEMPTS} attempt(s)"
            ) from last_error

    # -- public API ----------------------------------------------------------

    async def disconnect(self) -> None:
        """Close the persistent connection.

        Acquires the session lock so it cannot sever an in-flight
        operation. Session state is reset synchronously before any await
        (0.1.x #31); the physical disconnect runs bounded in the
        background (0.1.x #32 pattern) so a resisting disconnect can never
        block the caller. Safe to call when already disconnected.
        """
        self._cancel_idle_task()
        async with self._session_lock:
            client = self._active_client
            self._reset_session_state()
            if client is not None:
                _LOGGER.debug(
                    "%s - Explicit disconnect of persistent connection",
                    self.mac_address,
                )
                self._schedule_background_disconnect(client)
