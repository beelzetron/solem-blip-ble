"""Solem BL-IP BLE client."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError

try:
    from bleak.exc import BleakGATTProtocolError
except ImportError:  # bleak < 3.0 (Home Assistant core ships 2.x)
    BleakGATTProtocolError = BleakDBusError  # type: ignore[misc, assignment]

_BLE_GATT_ERRORS: tuple[type[Exception], ...] = (
    BleakGATTProtocolError,
    BleakDBusError,
)
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from .const import (
    COMMIT_COMMAND,
    DEFAULT_BLUETOOTH_TIMEOUT,
    MAX_STATION_NUM,
    NOTIFY_CHAR_UUID,
    NOTIFY_PARTIAL_RETRY_DELAY,
    NOTIFY_SETTLE_DELAY,
    RECONNECT_DELAY,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
    SCAN_DURATION,
    SCAN_MAX_ROUNDS,
    SCAN_PAUSE,
    STATION_NAMES_IDLE_TIMEOUT,
    STATUS_NOTIFY_TIMEOUT,
    WRITE_CHAR_UUID,
)
from .exceptions import SolemConnectionError
from . import protocol

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class SolemClient:
    """Async BLE client for Solem BL-IP controllers."""

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
        self._conn_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._client: BleakClient | None = None
        self._ble_device: BLEDevice | None = ble_device
        self._had_client = False

    async def _resolve_ble_device(self) -> BLEDevice:
        if self._ble_device is not None:
            return self._ble_device

        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                return ble_device
            raise SolemConnectionError("Device not found! Failed connecting!")

        last_round = SCAN_MAX_ROUNDS - 1
        for round_idx in range(SCAN_MAX_ROUNDS):
            ble_device = await BleakScanner.find_device_by_address(
                self.mac_address, timeout=SCAN_DURATION
            )
            if ble_device is not None:
                self._ble_device = ble_device
                return ble_device

            devices = await BleakScanner.discover(timeout=SCAN_DURATION)
            for device in devices:
                if (device.address or "").lower() == self.mac_address.lower():
                    self._ble_device = device
                    return device

            if round_idx < last_round:
                await asyncio.sleep(SCAN_PAUSE)

        raise SolemConnectionError("Device not found! Failed connecting!")

    def _ble_device_callback(self) -> BLEDevice:
        if self._ble_device_resolver is not None:
            ble_device = self._ble_device_resolver()
            if ble_device is not None:
                self._ble_device = ble_device
                return ble_device
        if self._ble_device is None:
            raise SolemConnectionError("Device not found! Failed connecting!")
        return self._ble_device

    async def _establish_ble_connection(self) -> BleakClient:
        if self._had_client:
            await asyncio.sleep(RECONNECT_DELAY)

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
                **connect_kwargs,
            )
        except BleakOutOfConnectionSlotsError as exc:
            raise SolemConnectionError(
                "Bluetooth adapter/proxy out of connection slots or device busy"
            ) from exc
        except (BleakDBusError, TimeoutError, OSError) as exc:
            raise SolemConnectionError("Timeout connecting to device") from exc
        except Exception as exc:
            raise SolemConnectionError("Unexpected BLE connection error") from exc

    async def _drop_client_unsafe(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    async def _invalidate_session(self) -> None:
        async with self._conn_lock:
            self._ble_device = None
            await self._drop_client_unsafe()

    async def _get_connected_client(self) -> BleakClient:
        async with self._conn_lock:
            if self._client is not None and self._client.is_connected:
                return self._client

            if self._client is not None:
                self._had_client = True
            await self._drop_client_unsafe()
            client = await self._establish_ble_connection()
            self._client = client
            self._had_client = True
            return client

    async def disconnect(self) -> None:
        """Close the persistent BLE session."""
        async with self._operation_lock:
            async with self._conn_lock:
                self._ble_device = None
                self._had_client = False
                await self._drop_client_unsafe()

    async def _run_with_client(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
    ) -> _T:
        async with self._operation_lock:
            try:
                client = await self._get_connected_client()
                if not client.is_connected:
                    raise SolemConnectionError("Failed connecting!")
                return await operation(client)
            except RetryError as exc:
                await self._invalidate_session()
                raise SolemConnectionError("BLE operation failed after retries") from exc
            except SolemConnectionError:
                await self._invalidate_session()
                raise
            except _BLE_GATT_ERRORS as exc:
                await self._invalidate_session()
                raise SolemConnectionError("BLE GATT error during device operation") from exc

    async def _ensure_connected(self, client: BleakClient, *, phase: str) -> None:
        if not client.is_connected:
            raise SolemConnectionError(f"Client disconnected before {phase}")

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        await self._ensure_connected(client, phase="write")
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)
        except _BLE_GATT_ERRORS as exc:
            raise SolemConnectionError("BLE write failed") from exc

    async def _start_notify(
        self,
        client: BleakClient,
        handler: Callable[[Any, bytearray], None],
    ) -> None:
        """Subscribe to status notifications with settle time and retries."""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if attempt == 0:
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                else:
                    await asyncio.sleep(NOTIFY_PARTIAL_RETRY_DELAY)
                await client.start_notify(NOTIFY_CHAR_UUID, handler)
                return
            except _BLE_GATT_ERRORS as exc:
                last_exc = exc
                _LOGGER.debug(
                    "%s - start_notify attempt %s failed: %s",
                    self.mac_address,
                    attempt + 1,
                    exc,
                )
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception:
                    pass
        raise SolemConnectionError(
            "Failed to subscribe to status notifications"
        ) from last_exc

    async def _execute_command(
        self,
        command: bytes,
    ) -> protocol.SolemStatus | None:
        """Send command + commit and wait for device notification ack."""
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

        async def _op(client: BleakClient) -> protocol.SolemStatus | None:
            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            await self._ensure_connected(client, phase="command")
            try:
                await self._write(client, command)
                await self._write(client, protocol.pack_commit())
                try:
                    await asyncio.wait_for(
                        response_event.wait(), timeout=STATUS_NOTIFY_TIMEOUT
                    )
                except asyncio.TimeoutError as exc:
                    raise SolemConnectionError(
                        "Timeout waiting for command response"
                    ) from exc
                return last_status
            finally:
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception as exc:
                    _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

        return await self._run_with_client(_op)

    async def _write_and_commit(self, command: bytes) -> protocol.SolemStatus | None:
        return await self._execute_command(command)

    async def connect(self) -> None:
        """Verify the device is reachable and exposes the write characteristic."""
        if self.mock:
            return

        async def _op(client: BleakClient) -> None:
            services = getattr(client, "services", None)
            if services is None:
                raise SolemConnectionError("Services not available on BLE client")
            for service in services:
                for char in service.characteristics:
                    if str(char.uuid).lower() == WRITE_CHAR_UUID.lower():
                        if "write" in char.properties or "write-without-response" in char.properties:
                            return
            raise SolemConnectionError("Device isn't suitable!")

        await self._run_with_client(_op)

    async def get_status(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Poll status via commit (triggers seq 0x02 notification)."""
        if self.mock:
            return protocol.mock_status()

        @retry(
            stop=stop_after_attempt(REQUEST_MAX_ATTEMPTS),
            wait=wait_fixed(REQUEST_RETRY_DELAY),
            retry=retry_if_exception_type(SolemConnectionError),
            reraise=True,
        )
        async def _attempt() -> dict[str, Any]:
            return await self._get_status_once(include_raw=include_raw)

        return await _attempt()

    async def _get_status_once(self, *, include_raw: bool = False) -> dict[str, Any]:
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

        async def _op(client: BleakClient) -> dict[str, Any]:
            await self._start_notify(client, notification_handler)
            await asyncio.sleep(NOTIFY_SETTLE_DELAY)
            await self._ensure_connected(client, phase="status poll")
            try:
                await self._write(client, COMMIT_COMMAND)
                try:
                    await asyncio.wait_for(
                        status_event.wait(), timeout=STATUS_NOTIFY_TIMEOUT
                    )
                except asyncio.TimeoutError as exc:
                    raise SolemConnectionError(
                        "Timeout waiting for status notification"
                    ) from exc
                if not status_result:
                    raise SolemConnectionError("Empty status notification")
                # Device sends seq 0x02 then 0x01; wait briefly for intermediate frame.
                if (
                    status_result.get("is_watering")
                    and status_result.get("remaining_seconds") is None
                    and (status_result.get("station_num") or 0) >= 3
                ):
                    await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                return status_result
            finally:
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception as exc:
                    _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

        return await self._run_with_client(_op)

    async def get_firmware_version(self) -> protocol.FirmwareVersion:
        """Read the firmware version stored on the V5 controller."""
        request = protocol.pack_get_firmware_version()
        if self.mock:
            return {"major": 5, "minor": 0, "patch": 0, "raw_hex": "5.0.0"}

        @retry(
            stop=stop_after_attempt(REQUEST_MAX_ATTEMPTS),
            wait=wait_fixed(REQUEST_RETRY_DELAY),
            retry=retry_if_exception_type(SolemConnectionError),
            reraise=True,
        )
        async def _attempt() -> protocol.FirmwareVersion:
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

            async def _op(client: BleakClient) -> protocol.FirmwareVersion:
                await self._start_notify(client, notification_handler)
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                await self._ensure_connected(client, phase="firmware version read")
                try:
                    await self._write(client, request)
                    try:
                        await asyncio.wait_for(
                            firmware_event.wait(), timeout=STATUS_NOTIFY_TIMEOUT
                        )
                    except asyncio.TimeoutError as exc:
                        raise SolemConnectionError(
                            "Timeout waiting for firmware version"
                        ) from exc
                    if firmware_version is None:
                        raise SolemConnectionError("Empty firmware version response")
                    return firmware_version
                finally:
                    try:
                        await client.stop_notify(NOTIFY_CHAR_UUID)
                    except Exception as exc:
                        _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

            return await self._run_with_client(_op)

        return await _attempt()

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

        @retry(
            stop=stop_after_attempt(REQUEST_MAX_ATTEMPTS),
            wait=wait_fixed(REQUEST_RETRY_DELAY),
            retry=retry_if_exception_type(SolemConnectionError),
            reraise=True,
        )
        async def _attempt() -> dict[int, str]:
            fragments: dict[int, dict[int, bytes]] = {}
            station_names: dict[int, str] = {}
            last_fragment_at: float | None = None

            def notification_handler(_sender: int, data: bytearray) -> None:
                nonlocal last_fragment_at
                parsed = protocol.parse_station_name_fragment(data)
                if parsed is None or not 1 <= parsed["station"] <= self.max_station_num:
                    return
                last_fragment_at = time.monotonic()
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
                deadline = time.monotonic() + STATUS_NOTIFY_TIMEOUT
                while True:
                    if len(station_names) == self.max_station_num:
                        return station_names
                    now = time.monotonic()
                    if (
                        station_names
                        and last_fragment_at is not None
                        and now - last_fragment_at >= STATION_NAMES_IDLE_TIMEOUT
                    ):
                        return station_names
                    if now >= deadline:
                        if station_names:
                            return station_names
                        raise SolemConnectionError(
                            "Timeout waiting for station names"
                        )
                    await asyncio.sleep(0.05)

            async def _op(client: BleakClient) -> dict[int, str]:
                await self._start_notify(client, notification_handler)
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                await self._ensure_connected(client, phase="station names read")
                try:
                    await self._write(client, request)
                    return await _wait_for_station_names()
                finally:
                    try:
                        await client.stop_notify(NOTIFY_CHAR_UUID)
                    except Exception as exc:
                        _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

            return await self._run_with_client(_op)

        return await _attempt()

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

        @retry(
            stop=stop_after_attempt(REQUEST_MAX_ATTEMPTS),
            wait=wait_fixed(REQUEST_RETRY_DELAY),
            retry=retry_if_exception_type(SolemConnectionError),
            reraise=True,
        )
        async def _attempt() -> dict[int, protocol.IrrigationProgram]:
            payloads: list[bytes] = []
            config_event = asyncio.Event()

            def notification_handler(_sender: int, data: bytearray) -> None:
                payload = bytes(data)
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
                if protocol.irrigation_config_complete(payloads):
                    config_event.set()

            async def _op(client: BleakClient) -> dict[int, protocol.IrrigationProgram]:
                await self._start_notify(client, notification_handler)
                await asyncio.sleep(NOTIFY_SETTLE_DELAY)
                await self._ensure_connected(client, phase="irrigation config read")
                try:
                    await self._write(client, request)
                    try:
                        await asyncio.wait_for(
                            config_event.wait(), timeout=STATUS_NOTIFY_TIMEOUT
                        )
                    except asyncio.TimeoutError as exc:
                        raise SolemConnectionError(
                            "Timeout waiting for irrigation config"
                        ) from exc
                    programs = protocol.assemble_irrigation_programs(
                        payloads, max_stations=self.max_station_num
                    )
                    if not protocol.irrigation_config_complete(payloads):
                        raise SolemConnectionError(
                            "Incomplete irrigation config response"
                        )
                    return programs
                finally:
                    try:
                        await client.stop_notify(NOTIFY_CHAR_UUID)
                    except Exception as exc:
                        _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

            return await self._run_with_client(_op)

        return await _attempt()

    async def set_time(self, when: datetime | None = None) -> None:
        """Push local date/time to the device RTC (write-only, no commit)."""
        if self.mock:
            return

        payload = protocol.pack_set_time(when)

        async def _op(client: BleakClient) -> None:
            await self._write(client, payload)

        await self._run_with_client(_op)

    async def turn_on(self) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_turn_on())

    async def turn_off_permanent(self) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_turn_off_permanent())

    async def turn_off_x_days(self, days: int) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_turn_off_x_days(days))

    async def sprinkle_station_x_for_y_minutes(
        self, station: int, minutes: int
    ) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._write_and_commit(
            protocol.pack_sprinkle_station(station, minutes)
        )

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> protocol.SolemStatus | None:
        if self.mock:
            return None
        return await self._write_and_commit(
            protocol.pack_sprinkle_all_stations(minutes)
        )

    async def run_program_x(self, program: int) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_run_program(program))

    async def stop_manual_sprinkle(self) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_stop_manual_sprinkle())

    async def list_characteristics(self) -> dict[str, list[dict[str, Any]]]:
        """Return discovered GATT services/characteristics (debug)."""
        if self.mock:
            return {}

        result: dict[str, list[dict[str, Any]]] = {}

        async def _op(client: BleakClient) -> dict[str, list[dict[str, Any]]]:
            services = getattr(client, "services", None)
            if services is None:
                raise SolemConnectionError("Services not available on BLE client")
            for svc in services:
                chars = []
                for char in svc.characteristics:
                    chars.append(
                        {
                            "uuid": str(char.uuid),
                            "properties": list(char.properties),
                            "descriptors": [str(d.uuid) for d in char.descriptors],
                        }
                    )
                result[str(svc.uuid)] = chars
            return result

        return await self._run_with_client(_op)
