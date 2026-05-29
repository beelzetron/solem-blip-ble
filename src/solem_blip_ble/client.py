"""Solem BL-IP BLE client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakGATTProtocolError
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
    wait_exponential,
)

from .const import (
    COMMIT_COMMAND,
    DEFAULT_BLUETOOTH_TIMEOUT,
    DEFAULT_MAX_STATION_NUM,
    NOTIFY_CHAR_UUID,
    NOTIFY_SETTLE_DELAY,
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
    ) -> None:
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.mock = mock
        self.max_station_num = max_station_num
        self._conn_lock = asyncio.Lock()

    async def _resolve_ble_device(self) -> BLEDevice:
        ble_device = await BleakScanner.find_device_by_address(
            self.mac_address, timeout=5.0
        )
        if ble_device is not None:
            return ble_device

        devices = await BleakScanner.discover(timeout=5.0)
        for device in devices:
            if (device.address or "").lower() == self.mac_address.lower():
                return device

        raise SolemConnectionError("Device not found! Failed connecting!")

    async def _connect_client(self) -> BleakClient:
        async with self._conn_lock:
            ble_device = await self._resolve_ble_device()
            try:
                return await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    name=f"Solem - {self.mac_address}",
                    timeout=self.bluetooth_timeout,
                    max_attempts=3,
                )
            except BleakOutOfConnectionSlotsError as exc:
                raise SolemConnectionError(
                    "Bluetooth adapter/proxy out of connection slots or device busy"
                ) from exc
            except (BleakDBusError, TimeoutError, OSError) as exc:
                raise SolemConnectionError("Timeout connecting to device") from exc
            except Exception as exc:
                raise SolemConnectionError("Unexpected BLE connection error") from exc

    async def _run_with_client(
        self,
        operation: Callable[[BleakClient], Awaitable[_T]],
    ) -> _T:
        client = await self._connect_client()
        try:
            if not client.is_connected:
                raise SolemConnectionError("Failed connecting!")
            return await operation(client)
        except RetryError as exc:
            raise SolemConnectionError("BLE operation failed after retries") from exc
        except (BleakGATTProtocolError, BleakDBusError) as exc:
            raise SolemConnectionError("BLE GATT error during device operation") from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _ensure_connected(self, client: BleakClient, *, phase: str) -> None:
        if not client.is_connected:
            raise SolemConnectionError(f"Client disconnected before {phase}")

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        await self._ensure_connected(client, phase="write")
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)
        except (BleakGATTProtocolError, BleakDBusError) as exc:
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
                await asyncio.sleep(NOTIFY_SETTLE_DELAY if attempt == 0 else 0.5 * attempt)
                await client.start_notify(NOTIFY_CHAR_UUID, handler)
                return
            except (BleakGATTProtocolError, BleakDBusError) as exc:
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

    async def _write_and_commit(self, command: bytes) -> None:
        async def _op(client: BleakClient) -> None:
            await self._write(client, command)
            await self._write(client, protocol.pack_commit())

        await self._run_with_client(_op)

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
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=0.5, min=1.0, max=5),
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
                    await asyncio.wait_for(status_event.wait(), timeout=5.0)
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

    async def sprinkle_station_x_for_y_minutes(self, station: int, minutes: int) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_sprinkle_station(station, minutes))

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> None:
        if self.mock:
            return
        await self._write_and_commit(protocol.pack_sprinkle_all_stations(minutes))

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
