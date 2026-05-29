"""Solem BL-IP BLE client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError

try:
    from bleak.exc import BleakGATTProtocolError
except ImportError:  # bleak < 3.0 (Home Assistant core ships 2.x)
    BleakGATTProtocolError = BleakDBusError

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
    DEFAULT_MAX_STATION_NUM,
    NOTIFY_CHAR_UUID,
    NOTIFY_PARTIAL_RETRY_DELAY,
    NOTIFY_SETTLE_DELAY,
    RECONNECT_DELAY,
    REQUEST_MAX_ATTEMPTS,
    REQUEST_RETRY_DELAY,
    SCAN_DURATION,
    SCAN_MAX_ROUNDS,
    SCAN_PAUSE,
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
        max_station_num: int = DEFAULT_MAX_STATION_NUM,
        ble_device: BLEDevice | None = None,
        ble_device_resolver: Callable[[], BLEDevice | None] | None = None,
    ) -> None:
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = mock
        self.max_station_num = max_station_num
        self._ble_device_resolver = ble_device_resolver
        self._conn_lock = asyncio.Lock()
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
        async with self._conn_lock:
            self._ble_device = None
            self._had_client = False
            await self._drop_client_unsafe()

    async def _run_with_client(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
    ) -> _T:
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
        handler: Callable[[int, bytearray], None],
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
    ) -> dict[str, Any] | None:
        """Send command + commit and wait for device notification ack (observed BLE behavior flow)."""
        response_event = asyncio.Event()
        last_status: dict[str, Any] | None = None

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
            response_event.set()

        async def _op(client: BleakClient) -> dict[str, Any] | None:
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

    async def _write_and_commit(self, command: bytes) -> dict[str, Any] | None:
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
            if parsed is None:
                return
            status_result.update(parsed)
            if include_raw:
                status_result["raw_notification_hex"] = bytes(data).hex()
            _LOGGER.debug(
                "%s - Status notification (seq=2): %s",
                self.mac_address,
                status_result,
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
                return status_result
            finally:
                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception as exc:
                    _LOGGER.debug("%s - stop_notify: %s", self.mac_address, exc)

        return await self._run_with_client(_op)

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
    ) -> dict[str, Any] | None:
        if self.mock:
            return None
        return await self._write_and_commit(
            protocol.pack_sprinkle_station(station, minutes)
        )

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> dict[str, Any] | None:
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
