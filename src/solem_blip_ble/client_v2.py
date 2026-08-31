"""Stateless connect-per-operation BLE client for Solem BL-IP controllers.

v2 connection layer (beelzetron/solem-blip-ble#38). Unlike the 0.1.x
persistent-session client, this module keeps **no session state**: every
public operation resolves the device, connects, runs the protocol exchange,
and closes the client — always, including on failure.

Guarantees, by construction rather than by patching:

- **Bounded worst case**: a whole-operation deadline (30 s) wraps every
  internal retry, so a dead link fails in bounded time.
- **Active disconnect detection**: the backend disconnect callback
  short-circuits in-flight waits immediately.
- **Flat bounded retry**: one small retry loop around the whole operation
  for transient errors only. No nested retry layers, no tenacity.
- **No stale state**: nothing survives an operation except parsed data.
  Stale-client reuse and poisoned-release bugs are impossible by design.

The public API mirrors the 0.1.x ``SolemClient`` so consumers migrate by
changing the import, not the calls. ``protocol.py`` is reused unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from . import protocol
from .const import (
    BLE_DEVICE_CACHE_TTL,
    COMMIT_COMMAND,
    DEFAULT_BLUETOOTH_TIMEOUT,
    IRRIGATION_CONFIG_IDLE_TIMEOUT,
    MAX_STATION_NUM,
    NOTIFY_CHAR_UUID,
    NOTIFY_PARTIAL_RETRY_DELAY,
    NOTIFY_SETTLE_DELAY,
    OPERATION_DEADLINE,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
    STATION_NAMES_IDLE_TIMEOUT,
    STATUS_NOTIFY_TIMEOUT,
    WRITE_CHAR_UUID,
)
from .exceptions import SolemConnectionError, SolemDeadlineExceeded

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

DISCONNECT_CLEANUP_TIMEOUT = 3.0

# Backend-level errors. BleakError is the base class of every backend error
# family (BleakDBusError, BleakGATTProtocolError, bleak-esphome wrappers), so
# catching the base covers all current and future backend error types.
_BACKEND_ERRORS = (BleakError, TimeoutError, OSError)


class _DropDetected(Exception):
    """Internal: the link dropped mid-operation; do not retry."""


async def _await_operation(awaitable: Awaitable[_T]) -> _T:
    return await awaitable


class StatelessSolemClient:
    """Connect-per-operation BLE client for a single Solem BL-IP controller.

    Public API mirrors ``solem_blip_ble.client.SolemClient`` (0.1.x) so the
    integration can migrate by changing the import.
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
    ) -> None:
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = mock
        self.max_station_num = max_station_num
        self._ble_device_resolver = ble_device_resolver
        self._ble_device: BLEDevice | None = ble_device
        self._ble_device_cached_at: float | None = None
        self._link_dropped = False
        self._drop_event = asyncio.Event()
        self._connection_epoch = 0

    # -- device resolution -------------------------------------------------

    async def _resolve_ble_device(self) -> BLEDevice:
        """Resolve a fresh BLEDevice, honouring the cache TTL."""
        if (
            self._ble_device is not None
            and self._ble_device_cached_at is not None
            and time.monotonic() - self._ble_device_cached_at < BLE_DEVICE_CACHE_TTL
        ):
            return self._ble_device
        self._ble_device = None

        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device
            raise SolemConnectionError("Device not found! Failed connecting!")

        last_round = 2
        for round_idx in range(3):
            ble_device = await BleakScanner.find_device_by_address(
                self.mac_address, timeout=10.0
            )
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device

            devices = await BleakScanner.discover(timeout=10.0)
            for device in devices:
                if (device.address or "").lower() == self.mac_address.lower():
                    self._ble_device = device
                    self._ble_device_cached_at = time.monotonic()
                    return device

            if round_idx < last_round:
                await asyncio.sleep(1.0)

        raise SolemConnectionError("Device not found! Failed connecting!")

    def _ble_device_callback(self) -> BLEDevice:
        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                self._ble_device_cached_at = time.monotonic()
                return ble_device
        if self._ble_device is None:
            raise SolemConnectionError("Device not found! Failed connecting!")
        return self._ble_device

    # -- connection core ---------------------------------------------------

    def _make_disconnect_callback(self) -> Callable[[BleakClient], None]:
        """Build a disconnect callback bound to the new connection's epoch.

        Backends deliver ``disconnected_callback`` through wrapper objects
        (service cache, ESPHome backend clients) whose identity is not
        guaranteed to be connection-unique, so the callback is gated by a
        per-connection epoch counter instead of client identity: without
        gating, a *stale* connection's late callback would falsely kill a
        healthy newer operation (observed on live hardware as back-to-back
        reads failing with "BLE link dropped during operation").
        """
        self._connection_epoch += 1
        epoch = self._connection_epoch

        def _on_disconnected(_client: BleakClient) -> None:
            if self._connection_epoch != epoch:
                return
            self._link_dropped = True
            self._drop_event.set()

        return _on_disconnected

    async def _connect(self) -> BleakClient:
        """Connect once, with the disconnect callback armed for short-circuiting."""
        ble_device = await self._resolve_ble_device()
        connect_kwargs: dict[str, Any] = {}
        if self._ble_device_resolver is not None:
            connect_kwargs["ble_device_callback"] = self._ble_device_callback
        try:
            return await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                name=f"Solem - {self.mac_address}",
                timeout=self.bluetooth_timeout,
                max_attempts=3,
                disconnected_callback=self._make_disconnect_callback(),
                **connect_kwargs,
            )
        except BleakOutOfConnectionSlotsError as exc:
            raise SolemConnectionError(
                "Bluetooth adapter/proxy out of connection slots or device busy"
            ) from exc
        except (BleakError, TimeoutError, OSError) as exc:
            raise SolemConnectionError("Failed connecting to device") from exc
        except Exception as exc:
            raise SolemConnectionError("Unexpected BLE connection error") from exc

    # -- the stateless executor --------------------------------------------

    def _check_drop(self) -> None:
        """Raise if the link dropped mid-operation."""
        if self._link_dropped:
            raise _DropDetected()

    async def _disconnect_quietly(self, client: BleakClient) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("%s - Disconnect cleanup: %s", self.mac_address, exc)

    async def _run_operation(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
        *,
        deadline: float | None = None,
    ) -> _T:
        """Resolve, connect, run, close. Bounded, flat-retried, stateless.

        The deadline covers the whole operation including all retries. In
        the stateless model every attempt starts from a fresh connection,
        so the flat loop retries any failure and the deadline alone stops
        it. A mid-operation link drop is the one fatal case: the retry
        would race into the freshly-dropped link, so it surfaces as
        ``SolemConnectionError`` immediately.
        """
        if self.mock:
            raise SolemConnectionError("mock client has no BLE operations")

        if deadline is None:
            # Read at call time so monkeypatching the module constant works.
            deadline = OPERATION_DEADLINE
        deadline_at = time.monotonic() + deadline
        last_error: Exception | None = None
        self._drop_event.clear()
        self._link_dropped = False

        try:
            for attempt in range(1, REQUEST_MAX_ATTEMPTS + 1):
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise SolemDeadlineExceeded(
                        f"Operation deadline exceeded after {attempt - 1} attempt(s)"
                    ) from last_error

                client: BleakClient | None = None
                op_task: asyncio.Task[_T] | None = None
                drop_task: asyncio.Task[Any] | None = None
                try:
                    client = await asyncio.wait_for(
                        self._connect(), timeout=remaining
                    )
                    self._connection_epoch += 1
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        raise SolemDeadlineExceeded(
                            "Operation deadline exhausted during connect"
                        )
                    op_task = asyncio.create_task(
                        _await_operation(operation(client))
                    )
                    drop_task = asyncio.create_task(self._drop_event.wait())
                    done, _ = await asyncio.wait(
                        {op_task, drop_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if drop_task in done:
                        raise _DropDetected()
                    if op_task not in done:
                        raise asyncio.TimeoutError(
                            f"Operation deadline exceeded during attempt {attempt}"
                        )
                    return op_task.result()
                except asyncio.CancelledError:
                    raise
                except SolemDeadlineExceeded:
                    raise
                except (asyncio.TimeoutError, BleakError, OSError, SolemConnectionError) as exc:
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
                    if client is not None:
                        # Retire this connection's epoch BEFORE awaiting the
                        # intentional close: a disconnect callback firing
                        # during teardown then targets a stale epoch and is
                        # ignored, while callbacks from a superseded attempt
                        # (retry already bumped the epoch) are ignored too.
                        self._connection_epoch += 1
                        self._link_dropped = False
                        self._drop_event.clear()
                        await self._disconnect_quietly(client)

                if (
                    time.monotonic() < deadline_at
                    and attempt < REQUEST_MAX_ATTEMPTS
                ):
                    await asyncio.sleep(REQUEST_RETRY_DELAY)

            raise SolemDeadlineExceeded(
                f"Operation deadline exceeded after {REQUEST_MAX_ATTEMPTS} attempt(s)"
            ) from last_error
        except _DropDetected as exc:
            raise SolemConnectionError(
                "BLE link dropped during operation"
            ) from exc

    # -- shared operation helpers ------------------------------------------

    def _ensure_client(self, client: BleakClient, phase: str) -> None:
        self._check_drop()
        if not client.is_connected:
            raise SolemConnectionError(f"Client disconnected before {phase}")

    async def _start_notify(
        self,
        client: BleakClient,
        handler: Callable[[Any, bytearray], None],
    ) -> None:
        """Subscribe to status notifications with settle time and retries."""
        last_exc: Exception | None = None
        for attempt in range(3):
            self._check_drop()
            try:
                if attempt == 0:
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                else:
                    await asyncio.sleep(NOTIFY_PARTIAL_RETRY_DELAY)
                self._check_drop()
                await client.start_notify(NOTIFY_CHAR_UUID, handler)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _LOGGER.debug(
                    "%s - start_notify attempt %s failed: %s",
                    self.mac_address,
                    attempt + 1,
                    exc,
                )
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception:  # noqa: BLE001
                    pass
        raise SolemConnectionError(
            "Failed to subscribe to status notifications"
        ) from last_exc

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        self._check_drop()
        if not client.is_connected:
            raise SolemConnectionError("Client disconnected before write")
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)
        except BleakError as exc:
            raise SolemConnectionError("BLE write failed") from exc

    async def _stop_notify(self, client: BleakClient) -> None:
        try:
            await client.stop_notify(NOTIFY_CHAR_UUID)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

    async def _wait_for_event(
        self,
        event: asyncio.Event,
        timeout: float,
        what: str,
    ) -> None:
        """Wait on an event, aborting early if the link dropped."""
        wait_task = asyncio.create_task(event.wait())
        stage_deadline = time.monotonic() + timeout
        try:
            while not event.is_set():
                self._check_drop()
                remaining = stage_deadline - time.monotonic()
                if remaining <= 0:
                    raise SolemConnectionError(f"Timeout waiting for {what}")
                done, _ = await asyncio.wait(
                    {wait_task}, timeout=min(remaining, 0.5)
                )
                if wait_task in done:
                    return
                self._check_drop()
        finally:
            wait_task.cancel()

    # -- public API (mirrors 0.1.x SolemClient) ----------------------------

    async def connect(self) -> None:
        """Verify the device is reachable and exposes the write characteristic."""

        async def _op(client: BleakClient) -> None:
            services = getattr(client, "services", None)
            if services is None:
                raise SolemConnectionError("Services not available on BLE client")
            for service in services:
                for char in service.characteristics:
                    if str(char.uuid).lower() == WRITE_CHAR_UUID.lower():
                        if (
                            "write" in char.properties
                            or "write-without-response" in char.properties
                        ):
                            return
            raise SolemConnectionError("Device isn't suitable!")

        await self._run_operation(_op)

    async def get_status(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Poll status via commit (triggers seq 0x02 notification)."""
        if self.mock:
            return protocol.mock_status()

        async def _op(client: BleakClient) -> dict[str, Any]:
            status_result: dict[str, Any] = {}
            status_event = asyncio.Event()

            def notification_handler(_sender: int, data: bytearray) -> None:
                parsed = protocol.parse_status_notification(
                    data, max_station_num=self.max_station_num
                )
                if parsed is not None:
                    status_result.update(parsed)
                    if include_raw:
                        status_result["raw_notification_hex"] = bytes(data).hex()
                    _LOGGER.debug(
                        "%s - Status notification (seq=2): %s",
                        self.mac_address,
                        status_result,
                    )
                    status_event.set()
                    return

                station_num = status_result.get("station_num")
                if (
                    len(data) >= 3
                    and data[2] == 0x01
                    and status_result.get("is_watering")
                    and station_num is not None
                    and status_result.get("remaining_seconds") is None
                    and (
                        remaining := protocol.parse_intermediate_remaining(
                            data,
                            station_num,
                            max_station_num=self.max_station_num,
                        )
                    )
                    is not None
                ):
                    status_result["remaining_seconds"] = remaining
                    _LOGGER.debug(
                        "%s - Remaining time from seq=1 notification: %ss (station %s)",
                        self.mac_address,
                        remaining,
                        station_num,
                    )
                    status_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="status poll")
            try:
                await self._write(client, COMMIT_COMMAND)
                await self._wait_for_event(
                    status_event, STATUS_NOTIFY_TIMEOUT, "status notification"
                )
                if not status_result:
                    raise SolemConnectionError("Empty status notification")
                if (
                    status_result.get("is_watering")
                    and status_result.get("remaining_seconds") is None
                    and (status_result.get("station_num") or 0) >= 3
                ):
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                return status_result
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_firmware_version(self) -> protocol.FirmwareVersion:
        """Read the firmware version stored on the V5 controller."""
        request = protocol.pack_get_firmware_version()
        if self.mock:
            return {"major": 5, "minor": 0, "patch": 0, "raw_hex": "5.0.0"}

        async def _op(client: BleakClient) -> protocol.FirmwareVersion:
            firmware_version: protocol.FirmwareVersion | None = None
            firmware_event = asyncio.Event()

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal firmware_version
                parsed = protocol.parse_firmware_version_response(data)
                if parsed is None:
                    return
                firmware_version = parsed
                _LOGGER.debug(
                    "%s - Firmware version notification: %s",
                    self.mac_address,
                    bytes(data).hex(),
                )
                firmware_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="firmware version read")
            try:
                await self._write(client, request)
                await self._wait_for_event(
                    firmware_event, STATUS_NOTIFY_TIMEOUT, "firmware version"
                )
                if firmware_version is None:
                    raise SolemConnectionError("Empty firmware version response")
                return firmware_version
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_station_name(self, station: int) -> str:
        """Read a station name stored on the V5 controller."""
        if not 1 <= station <= self.max_station_num:
            raise ValueError(f"station must be between 1 and {self.max_station_num}")
        if self.mock:
            return f"Station {station}"
        return (await self.get_station_names())[station]

    async def get_station_names(self) -> dict[int, str]:
        """Read names for each configured station from the V5 controller."""
        request = protocol.pack_get_station_names()
        if self.mock:
            return {
                station: f"Station {station}"
                for station in range(1, self.max_station_num + 1)
            }

        async def _op(client: BleakClient) -> dict[int, str]:
            fragments: dict[int, dict[int, bytes]] = {}
            station_names: dict[int, str] = {}
            last_fragment_at: float | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_fragment_at
                parsed = protocol.parse_station_name_fragment(data)
                if parsed is None:
                    return
                last_fragment_at = time.monotonic()
                if not 1 <= parsed["station"] <= self.max_station_num:
                    return
                station = parsed["station"]
                fragments.setdefault(station, {})[parsed["sequence"]] = parsed[
                    "name_bytes"
                ]
                _LOGGER.debug(
                    "%s - Station %s name fragment (seq=%s): %s",
                    self.mac_address,
                    station,
                    parsed["sequence"],
                    bytes(data).hex(),
                )
                if fragments[station].keys() >= {0, 1}:
                    station_fragments = fragments[station]
                    station_names[station] = (
                        station_fragments[1] + station_fragments[0]
                    ).decode("utf-8", errors="replace")

            async def _wait_for_station_names() -> dict[int, str]:
                stage_deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
                while True:
                    now = time.monotonic()
                    if (
                        station_names
                        and last_fragment_at is not None
                        and now - last_fragment_at >= STATION_NAMES_IDLE_TIMEOUT
                    ):
                        return station_names
                    if now >= stage_deadline:
                        if station_names:
                            return station_names
                        raise SolemConnectionError(
                            "Timeout waiting for station names"
                        )
                    self._check_drop()
                    await asyncio.sleep(0.05)

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="station names read")
            try:
                await self._write(client, request)
                return await _wait_for_station_names()
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def get_irrigation_config(
        self,
    ) -> dict[int, protocol.IrrigationProgram]:
        """Read persisted irrigation programs (A/B/C) from the V5 controller."""
        request = protocol.pack_get_irrigation_config()
        if self.mock:
            return {
                program_index: {
                    "name": f"Program {chr(ord('A') + program_index)}",
                    "inter_station_delay": 0,
                    "water_budget": 100,
                    "cycle": 0,
                    "week_days": 0x7F,
                    "period_length": 1,
                    "synchro_day": 0,
                    "period_start_date": None,
                    "start_times": [420 + program_index * 60, None, None, None, None, None, None, None],
                    "station_durations": [
                        600 if station == 0 else 0
                        for station in range(self.max_station_num)
                    ],
                }
                for program_index in range(protocol.IRRIGATION_PROGRAM_COUNT)
            }

        async def _op(client: BleakClient) -> dict[int, protocol.IrrigationProgram]:
            payloads: list[bytes] = []
            last_fragment_at: float | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_fragment_at
                payload = bytes(data)
                normalized = protocol.normalize_config_notification(payload)
                if (
                    normalized is not None
                    and normalized[3] >> 4 == protocol.IRRIGATION_PROGRAM_CLASS
                ):
                    last_fragment_at = time.monotonic()
                parsed = protocol.parse_irrigation_config_fragment(payload)
                if parsed is None:
                    return
                payloads.append(payload)
                _LOGGER.debug(
                    "%s - Irrigation config fragment (program=%s, fragment=%s): %s",
                    self.mac_address,
                    parsed["program_index"],
                    parsed["fragment_id"],
                    payload.hex(),
                )

            async def _wait_for_irrigation_config() -> None:
                stage_deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
                while True:
                    now = time.monotonic()
                    if (
                        protocol.irrigation_config_complete(payloads)
                        and last_fragment_at is not None
                        and now - last_fragment_at >= IRRIGATION_CONFIG_IDLE_TIMEOUT
                    ):
                        return
                    if now >= stage_deadline:
                        if protocol.irrigation_config_complete(payloads):
                            return
                        raise SolemConnectionError(
                            "Timeout waiting for irrigation config"
                        )
                    self._check_drop()
                    await asyncio.sleep(0.05)

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="irrigation config read")
            try:
                await self._write(client, request)
                await _wait_for_irrigation_config()
                programs = protocol.assemble_irrigation_programs(
                    payloads, max_stations=self.max_station_num
                )
                if not protocol.irrigation_config_complete(payloads):
                    raise SolemConnectionError(
                        "Incomplete irrigation config response"
                    )
                return programs
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def set_irrigation_program(
        self,
        program_index: int,
        program: protocol.IrrigationProgram,
    ) -> dict[int, protocol.IrrigationProgram]:
        """Write one persisted V5 irrigation program and verify by reading it back."""
        frames = protocol.pack_set_irrigation_program(
            program_index,
            program,
            max_stations=self.max_station_num,
        )
        expected = protocol.normalize_irrigation_program_for_write(
            program,
            max_stations=self.max_station_num,
        )

        if self.mock:
            programs = await self.get_irrigation_config()
            programs[program_index] = expected
            return programs

        async def _op(client: BleakClient) -> None:
            for frame in frames:
                self._check_drop()
                await self._write(client, frame)

        await self._run_operation(_op)
        programs = await self.get_irrigation_config()
        written = programs.get(program_index)
        mismatches = protocol.irrigation_program_write_mismatches(written, expected)
        if mismatches:
            details = ", ".join(
                f"{field}: expected {expected_value!r}, got {actual_value!r}"
                for field, (expected_value, actual_value) in mismatches.items()
            )
            raise SolemConnectionError(
                f"Irrigation program write verification failed ({details})"
            )
        return programs

    async def set_time(self, when: datetime | None = None) -> None:
        """Push local date/time to the device RTC (write-only, no commit)."""
        if self.mock:
            return

        payload = protocol.pack_set_time(when)

        async def _op(client: BleakClient) -> None:
            await self._write(client, payload)

        await self._run_operation(_op)

    async def _execute_command(
        self,
        command: bytes,
    ) -> protocol.SolemStatus | None:
        """Send command + commit and wait for device notification ack."""
        if self.mock:
            return None

        async def _op(client: BleakClient) -> protocol.SolemStatus | None:
            response_event = asyncio.Event()
            last_status: protocol.SolemStatus | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_status
                if not protocol.is_command_notification(data):
                    return
                parsed = protocol.parse_status_notification(
                    data, max_station_num=self.max_station_num
                )
                if parsed is not None:
                    last_status = parsed
                _LOGGER.debug(
                    "%s - Command notification (seq=%s): %s",
                    self.mac_address,
                    data[2],
                    bytes(data).hex(),
                )
                if data[2] == 0x00:
                    response_event.set()

            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            self._ensure_client(client, phase="command")
            try:
                await self._write(client, command)
                await self._write(client, protocol.pack_commit())
                await self._wait_for_event(
                    response_event, STATUS_NOTIFY_TIMEOUT, "command response"
                )
                return last_status
            finally:
                await self._stop_notify(client)

        return await self._run_operation(_op)

    async def turn_on(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_on())

    async def turn_off_permanent(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_off_permanent())

    async def turn_off_x_days(self, days: int) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_turn_off_x_days(days))

    async def sprinkle_station_x_for_y_minutes(
        self, station: int, minutes: int
    ) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._execute_command(
            protocol.pack_sprinkle_station(station, minutes)
        )

    async def sprinkle_all_stations_for_y_minutes(
        self, minutes: int
    ) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._execute_command(protocol.pack_sprinkle_all_stations(minutes))

    async def run_program_x(self, program: int) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_run_program(program))

    async def stop_manual_sprinkle(self) -> None:
        if self.mock:
            return
        await self._execute_command(protocol.pack_stop_manual_sprinkle())
