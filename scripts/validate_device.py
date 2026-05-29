#!/usr/bin/env python3
"""Validate solem-blip-ble against a real Solem BL-IP controller.

Read-only by default (connect + status poll). Use --actions to exercise
write commands on the device.

Examples:
  python3 scripts/validate_device.py
  SOLEM_MAC=C8:B9:61:D4:4D:C8 python3 scripts/validate_device.py
  python3 scripts/validate_device.py --actions --station 1 --minutes 1
  python3 scripts/validate_device.py --verbose --actions
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from solem_blip_ble import SolemClient, SolemConnectionError

try:
    from tenacity import RetryError
except ImportError:  # pragma: no cover - tenacity is a required dependency
    RetryError = Exception  # type: ignore[misc, assignment]

DEFAULT_MAC = "C8:B9:61:D4:4D:C8"
BLE_BUSY_HINT = (
    "Hint: stop Home Assistant / observed BLE behavior / other BLE clients using this controller, "
    "then retry. Upgrade with: pip install -U 'solem-blip-ble>=0.1.7'"
)


def _connection_detail(exc: BaseException) -> str:
    detail = str(exc)
    if isinstance(exc, RetryError):
        detail = "BLE operation failed after retries"
    if "not connected" in detail.lower() or isinstance(exc, RetryError):
        detail = f"{detail}. {BLE_BUSY_HINT}"
    return detail


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] | None = None


@dataclass
class RunReport:
    mac: str
    package_version: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(step.ok for step in self.steps)


def _format_status(status: dict[str, Any]) -> str:
    parts = [
        f"controller={status.get('controller_state')}",
        f"watering={status.get('is_watering')}",
    ]
    if status.get("station_num") is not None:
        parts.append(f"station={status.get('station_num')}")
    if status.get("remaining_seconds") is not None:
        parts.append(f"remaining={status.get('remaining_seconds')}s")
    if status.get("battery_voltage") is not None:
        parts.append(f"battery={status['battery_voltage']}V")
        if status.get("battery_level") is not None:
            parts.append(f"level={status['battery_level']}/5")
        if status.get("battery_low"):
            parts.append("battery_low")
    if status.get("raw_notification_hex"):
        parts.append(f"raw={status['raw_notification_hex']}")
    return ", ".join(parts)


def _print_step(step: StepResult) -> None:
    mark = "PASS" if step.ok else "FAIL"
    print(f"[{mark}] {step.name}")
    if step.data:
        print(f"       {_format_status(step.data)}")
    elif step.detail:
        print(f"       {step.detail}")


async def _run_read_only(
    client: SolemClient,
    report: RunReport,
    *,
    verbose: bool,
) -> None:
    for attempt in (1, 2):
        step = StepResult(f"get_status #{attempt}", False)
        try:
            status = await client.get_status(include_raw=verbose)
            step.ok = True
            step.data = status
        except (SolemConnectionError, RetryError) as exc:
            step.detail = _connection_detail(exc)
        report.steps.append(step)
        if not step.ok:
            return


async def _wait_for_watering(
    client: SolemClient,
    *,
    station: int,
    timeout: float,
    verbose: bool,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await client.get_status(include_raw=verbose)
        if status.get("is_watering") and status.get("station_num") == station:
            return status
        await asyncio.sleep(2.0)
    return None


async def _run_actions(
    client: SolemClient,
    report: RunReport,
    *,
    station: int,
    minutes: int,
    skip_sprinkle: bool,
    verbose: bool,
) -> None:
    step = StepResult("stop_manual_sprinkle (cleanup)", False)
    try:
        await client.stop_manual_sprinkle()
        step.ok = True
        step.detail = "Command sent"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.steps.append(step)

    if skip_sprinkle:
        report.steps.append(
            StepResult(
                "sprinkle test",
                True,
                detail="Skipped (--skip-sprinkle)",
            )
        )
        return

    step = StepResult(
        f"sprinkle_station_{station}_for_{minutes}_minutes",
        False,
    )
    try:
        await client.sprinkle_station_x_for_y_minutes(station, minutes)
        step.ok = True
        step.detail = "Command sent"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.steps.append(step)
    if not step.ok:
        return

    step = StepResult("verify watering status", False)
    try:
        status = await _wait_for_watering(
            client,
            station=station,
            timeout=20.0,
            verbose=verbose,
        )
        if status is None:
            step.detail = (
                f"Timed out waiting for station {station} to report watering"
            )
        else:
            remaining = status.get("remaining_seconds")
            expected = minutes * 60
            if remaining is None:
                step.detail = "Watering active but remaining_seconds missing"
            elif remaining > expected + 30:
                step.detail = (
                    f"remaining_seconds={remaining}s looks wrong "
                    f"(expected ~{expected}s for {minutes} min command)"
                )
            else:
                step.ok = True
                step.data = status
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.steps.append(step)

    step = StepResult("stop after sprinkle test", False)
    try:
        await client.stop_manual_sprinkle()
        step.ok = True
        step.detail = "Command sent"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.steps.append(step)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate solem-blip-ble on a real Solem BL-IP device",
    )
    parser.add_argument(
        "mac",
        nargs="?",
        default=os.environ.get("SOLEM_MAC", DEFAULT_MAC),
        help=f"Controller MAC (default: {DEFAULT_MAC} or SOLEM_MAC env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="BLE connection timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-station",
        type=int,
        default=6,
        help="Max station number for status parsing (default: 6)",
    )
    parser.add_argument(
        "--actions",
        action="store_true",
        help="Run write commands (stop, optional sprinkle test)",
    )
    parser.add_argument(
        "--station",
        type=int,
        default=1,
        help="Station for sprinkle test when --actions (default: 1)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=1,
        help="Minutes for sprinkle test when --actions (default: 1)",
    )
    parser.add_argument(
        "--skip-sprinkle",
        action="store_true",
        help="With --actions, only run stop (no sprinkle test)",
    )
    parser.add_argument(
        "--list-chars",
        action="store_true",
        help="List GATT services/characteristics after read-only checks",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include raw notification hex in status output",
    )
    args = parser.parse_args()

    mac = args.mac.upper()
    try:
        package_version = importlib.metadata.version("solem-blip-ble")
    except importlib.metadata.PackageNotFoundError:
        print("[FAIL] solem-blip-ble is not installed in this Python environment")
        print("       Run: pip install -e .")
        print("       Or:  pip install solem-blip-ble")
        return 1

    report = RunReport(mac=mac, package_version=package_version)
    client = SolemClient(
        mac,
        bluetooth_timeout=args.timeout,
        max_station_num=args.max_station,
    )

    try:
        print("solem-blip-ble device validation")
        print(f"Package:  solem-blip-ble {package_version}")
        print(f"MAC:      {mac}")
        print(f"Mode:     {'actions' if args.actions else 'read-only'}")
        print("-" * 60)

        await _run_read_only(client, report, verbose=args.verbose)

        if args.actions and report.steps and report.steps[0].ok:
            await _run_actions(
                client,
                report,
                station=args.station,
                minutes=args.minutes,
                skip_sprinkle=args.skip_sprinkle,
                verbose=args.verbose,
            )

        if args.list_chars:
            step = StepResult("list_characteristics", False)
            try:
                chars = await client.list_characteristics()
                step.ok = bool(chars)
                step.detail = f"{len(chars)} service(s)"
                if chars:
                    for service_uuid, characteristics in chars.items():
                        print(f"       service {service_uuid}")
                        for char in characteristics:
                            props = ", ".join(char["properties"])
                            print(f"         {char['uuid']} [{props}]")
            except (SolemConnectionError, RetryError) as exc:
                step.detail = _connection_detail(exc)
            report.steps.append(step)

        print("-" * 60)
        for step in report.steps:
            _print_step(step)

        print("-" * 60)
        if report.passed:
            print("Result: ALL CHECKS PASSED")
            return 0

        failed = [step.name for step in report.steps if not step.ok]
        print(f"Result: FAILED ({len(failed)} step(s): {', '.join(failed)})")
        return 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted")
        sys.exit(130)
    except RetryError as exc:
        print(f"[FAIL] {_connection_detail(exc)}")
        sys.exit(1)
