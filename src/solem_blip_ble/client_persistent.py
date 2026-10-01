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
- **Single subscription per connection** (0.3.2b10): on a held
  connection the notify CCCD is written exactly once and the subscription
  lives for the connection's lifetime; per-operation handlers are routed
  through an in-memory dispatcher instead of disable/re-enable cycles on
  the shared notify characteristic. FW5 controllers stop delivering
  notifications after a CCCD disable→re-enable cycle without reconnect
  (issue #136), so the stateless per-operation subscribe/unsubscribe is
  not safe to replay on a reused link.
- **Release-before-connect serialization** (0.3.2b10): a bounded
  background release is awaited before any new connection is established.
  bleak-esphome disconnects *by MAC address*, so a release still in
  flight when a retry reconnects tears down the fresh link instead of
  the dead one (#136: the ``Cannot notify GATT characteristic, not
  connected`` failures).
- **In-place retry of a silent name request** (0.3.2b11): the shared
  zero-frame verdict (:class:`~solem_blip_ble.exceptions.SolemSilentLink`)
  is answered with re-requests on the SAME held link — spaced by
  :data:`NAME_READ_IN_PLACE_DELAY` — before it is allowed to fail the
  operation. The FW 5.1.5 capture (#136) proved a link can be healthy
  yet silent to the ``35 00`` request; tearing it down then guarantees
  the next connect lands in the post-disconnect quiet window.

Single-connection device behavior: the BL-IP controller stops advertising
during and for tens of seconds after a connect attempt, so connect-phase
failures are **not** retried inside the same operation — the operation
fails immediately and the next poll reconnects. Operation-phase failures
(link drop mid-operation, write failure, notify timeout) keep the flat
retry, spaced by ``REQUEST_RETRY_DELAY``.

Connect-phase failure signatures (visible in the ``Attempt N failed:``
debug log, which previously logged an empty reason for the bare
``asyncio.TimeoutError`` raised by ``wait_for``):

- ``Connect phase timed out after Ns (device not advertising, out of
  range, or refusing connections)`` — the whole-operation deadline
  expired before the connect attempt returned
  (:class:`solem_blip_ble.client_v2._ConnectTimedOut`).
- ``Timeout connecting to device: {exc}`` — the backend connect attempt
  itself failed (BleakError/TimeoutError/OSError from
  ``establish_connection``); the underlying error text is included so
  log lines are self-describing.

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
    _ConnectTimedOut,
    _DropDetected,
    StatelessSolemClient,
)
# Imported as a module (not from-imported) so the straggler window always
# reads the same live constant the operations settle on.
from . import client_v2 as _v2
from .const import (
    DEFAULT_BLUETOOTH_TIMEOUT,
    MAX_STATION_NUM,
    OPERATION_DEADLINE,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
)
from .exceptions import SolemConnectionError, SolemDeadlineExceeded, SolemSilentLink
from .station_names import StationNameSnapshot

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Every teardown disconnect runs bounded in the background: a resisting
# disconnect must never block the next operation or the idle/disconnect
# path (0.1.x #31/#32 pattern).
RELEASE_DISCONNECT_TIMEOUT = 5.0

# In-place retry of a silent name request on a HELD link (issue #136).
# The FW 5.1.5 capture of 2026-09-30 showed the controller holding a
# healthy link (status notification seconds earlier) yet never answering
# the ``35 00`` request; tearing the session down for that verdict buys a
# post-disconnect quiet window in which the next connect is guaranteed to
# fail. The verdict is instead answered with re-requests on the same
# link, spaced by the delay, before it is allowed to fail the operation.
NAME_READ_IN_PLACE_RETRIES = 2
NAME_READ_IN_PLACE_DELAY = 5.0


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
        # Single-subscription routing state (0.3.2b10). The CCCD is written
        # once per connection; per-operation handlers are swapped in memory.
        self._dispatch_client: BleakClient | None = None
        self._active_handler: Callable[[Any, bytearray], None] | None = None
        # Frames arriving this soon after a handler swap can only be
        # stragglers of the previous operation (no operation writes its
        # command before the settle delay), so they are dropped instead of
        # being misdelivered to the new handler's buffers.
        self._straggler_drop_until = 0.0
        # Bounded background releases still in flight; awaited before any
        # new connection so a MAC-addressed disconnect can never reach a
        # fresh link (0.3.2b10).
        self._pending_releases: set[asyncio.Task[None]] = set()

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
        # The subscription dies with the connection; the next connect
        # re-subscribes for real.
        self._dispatch_client = None
        self._active_handler = None
        self._straggler_drop_until = 0.0

    def _dispatch_notification(
        self, sender: int, data: bytearray
    ) -> None:
        """Route one notification to the in-flight operation's handler.

        Called by bleak for every notification on the single shared notify
        characteristic. Frames with no active handler (between operations)
        or within the post-swap straggler window (responses to the previous
        operation) are dropped.
        """
        handler = self._active_handler
        if handler is None:
            return
        if time.monotonic() < self._straggler_drop_until:
            return
        handler(sender, data)

    async def _start_notify(
        self,
        client: BleakClient,
        handler: Callable[[Any, bytearray], None],
    ) -> None:
        """Subscribe once per connection; afterwards swap handlers only.

        Overrides the stateless per-operation subscribe/unsubscribe: on a
        held connection a CCCD disable→re-enable cycle makes FW5 stop
        delivering notifications entirely (issue #136), so the CCCD write
        happens exactly once per connection and the per-operation handler
        is installed in memory. The straggler window covers the settle
        delay, during which no operation has written its command yet —
        anything arriving there belongs to the previous operation.
        """
        if self._dispatch_client is client and client.is_connected:
            self._active_handler = handler
            # The window must cover exactly the operation's own pre-write
            # settle delay (ops sleep NOTIFY_SETTLE_DELAY between the swap
            # and their first write) — read it live from the client_v2
            # module so the invariant holds under test patching too.
            self._straggler_drop_until = (
                time.monotonic() + _v2.NOTIFY_SETTLE_DELAY
            )
            return
        # Real subscribe on a fresh connection. Clear routing state first:
        # notifications may start arriving while start_notify is awaited,
        # and frames before the operation's first write carry no data.
        self._dispatch_client = None
        self._active_handler = None
        await super()._start_notify(client, self._dispatch_notification)
        self._dispatch_client = client
        self._active_handler = handler
        self._straggler_drop_until = 0.0

    async def _stop_notify(self, client: BleakClient) -> None:
        """Keep the subscription; only stop routing to the finished op.

        The stateless client unsubscribes here (CCCD write), which on a
        reused connection is the first half of the FW5-killing
        disable→re-enable cycle (issue #136). The subscription instead
        lives until the connection is torn down; the CCCD state resets
        server-side on disconnect.
        """
        if self._dispatch_client is client:
            self._active_handler = None
            return
        await super()._stop_notify(client)

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
        self._pending_releases.add(task)
        task.add_done_callback(self._pending_releases.discard)
        task.add_done_callback(_consume_task_exception)

    async def _drain_pending_releases(self, timeout: float) -> None:
        """Await in-flight background releases before establishing a link.

        bleak-esphome disconnects *by MAC address*, so a release still
        running when a fresh connection is established tears down the new
        link instead of the dead one — the historic ``Cannot notify GATT
        characteristic, not connected`` retry failures (issue #136).
        Serializing release-then-connect makes the release unable to reach
        a link it does not own.
        """
        tasks = [t for t in self._pending_releases if not t.done()]
        if not tasks:
            return
        _LOGGER.debug(
            "%s - Awaiting %d pending release(s) before reconnect",
            self.mac_address,
            len(tasks),
        )
        await asyncio.wait(tasks, timeout=max(timeout, 0.0))

    # -- silent name-request recovery (issue #136) ---------------------------

    async def _read_station_names_on_connection(
        self, client: BleakClient, frames: list[bytes]
    ) -> StationNameSnapshot:
        """Answer a silent name request in place before tearing the link down.

        Issue #136 (FW 5.1.5, 2026-09-30 capture): the controller can hold
        a demonstrably healthy link — a status notification arrived on it
        seconds earlier — yet never answer the ``35 00`` request. The
        shared zero-frame verdict treats that as a dead link, and the
        resulting teardown is worse than the silence: on a
        single-connection controller the disconnect opens a
        post-disconnect quiet window in which the next connect is
        guaranteed to fail, so a link that was merely silent becomes one
        that is unreachable for tens of seconds.

        On a held connection the verdict is therefore first answered with
        an in-place re-request on the SAME link (after
        :data:`NAME_READ_IN_PLACE_DELAY`), up to
        :data:`NAME_READ_IN_PLACE_RETRIES` times — the request is a pure
        read, so re-issuing it with zero frames collected is stateless and
        safe. Only when the controller stays silent through every
        re-request does the original :class:`SolemSilentLink` verdict
        propagate to the executor, which invalidates the session and
        reconnects exactly as before. A confirmed-dead link
        (``is_connected`` False) or a partial/invalid response is never
        retried in place: those keep their precise stateless verdicts.
        """
        attempts_left = NAME_READ_IN_PLACE_RETRIES
        while True:
            try:
                return await super()._read_station_names_on_connection(
                    client, frames
                )
            except SolemSilentLink:
                if (
                    client is not self._active_client
                    or not client.is_connected
                    or attempts_left <= 0
                ):
                    raise
            attempts_left -= 1
            _LOGGER.debug(
                "%s - Station-name request unanswered on the held link; "
                "re-requesting in place (retry %d/%d)",
                self.mac_address,
                NAME_READ_IN_PLACE_RETRIES - attempts_left,
                NAME_READ_IN_PLACE_RETRIES,
            )
            await asyncio.sleep(NAME_READ_IN_PLACE_DELAY)
            if not client.is_connected:
                # The link died during the wait: hand the precise dead-link
                # verdict to the executor instead of another write.
                raise SolemConnectionError(
                    "BLE link dropped during station-name read"
                )

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
        retry_safe: bool = True,
    ) -> _T:
        """Run one operation over a held connection, keeping it on success.

        Same structure as the stateless executor: whole-operation deadline,
        flat retry loop, drop watcher racing the operation task. The only
        difference: the client is *not* disconnected on success — the link
        is reused by the next operation. On failure the session is
        invalidated (synchronously, then bounded background teardown) so
        the next attempt inside the same operation reconnects fresh.

        Single-connection devices: the controller stops advertising during
        and for tens of seconds after a connect attempt. A connect-phase
        failure (timeout, connection error) is therefore **not** retried
        within the operation — the operation raises immediately and the
        next poll reconnects. Operation-phase failures (link drop
        mid-operation, write failure, notify timeout) keep the flat retry
        with the spaced delay: those are worth retrying on a fresh
        session.
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
                        (
                            f"Operation deadline exceeded after {attempt - 1} attempt(s)"
                            f": {last_error}"
                            if last_error
                            else f"Operation deadline exceeded after {attempt - 1} attempt(s)"
                        )
                    ) from last_error

                reused = (
                    self._active_client is not None
                    and self._active_client.is_connected
                    and not self._link_dropped
                )
                op_task: asyncio.Task[_T] | None = None
                drop_task: asyncio.Task[None] | None = None
                connect_succeeded = False
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
                        # Never let an in-flight release (including the one
                        # just scheduled above) race the new connect: the
                        # backend disconnects by MAC and would kill the
                        # fresh link (issue #136).
                        await self._drain_pending_releases(remaining)
                        client = await self._connect_within(remaining)
                        self._active_client = client
                    connect_succeeded = True
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
                    if not retry_safe:
                        client_to_release = self._active_client
                        self._reset_session_state()
                        if client_to_release is not None:
                            self._schedule_background_disconnect(client_to_release)
                        raise
                    if not connect_succeeded:
                        # Connect-phase failure: either _ConnectTimedOut
                        # ('Connect phase timed out after Ns ...') or the
                        # 'Failed connecting to device' wrapper (or a bare
                        # TimeoutError from other backends). On a
                        # single-connection controller the device stops
                        # advertising during and for tens of seconds after
                        # a connect attempt, so an immediate retry within
                        # this operation is guaranteed to fail. Give up
                        # this operation; the next poll reconnects.
                        self._reset_session_state()
                        self._schedule_idle_release()
                        raise SolemDeadlineExceeded(
                            "Connect phase failed within operation deadline"
                            f"; deferring retry to the next operation: {exc}"
                        ) from exc
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
                (
                    f"Operation deadline exceeded after {REQUEST_MAX_ATTEMPTS} attempt(s)"
                    f": {last_error}"
                    if last_error
                    else "Operation deadline exceeded after "
                    f"{REQUEST_MAX_ATTEMPTS} attempt(s)"
                )
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
