"""Validate and troubleshoot all Solem BL-IP BLE library features on real hardware."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import sys
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any

from bleak import BleakScanner
from bleak.exc import BleakError

from solem_blip_ble import (
    IrrigationProgram,
    SolemClient,
    SolemConnectionError,
    assemble_irrigation_programs,
    irrigation_config_complete,
    parse_firmware_version_response,
    parse_station_name_fragment,
    parse_status_notification,
)
from solem_blip_ble.validate_common import (
    ALL_SECTIONS,
    BLE_BUSY_HINT,
    DEFAULT_CAPTURE_SECONDS,
    DEFAULT_SCHEDULE_CAPTURE_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    NOTIFY_CHAR_UUID,
    PROGRAM_LABELS,
    READ_SECTIONS,
    WRITE_CHAR_UUID,
    CaptureEvent,
    StepResult,
    action_capture_writes,
    action_listen_dwell_seconds,
    capture_probes,
    default_capture_output,
    describe_notification,
    format_minutes,
    format_status,
    load_capture_events,
    off_days_capture_writes,
    schedule_write_capture_writes,
    selected_sections,
    timestamp,
    write_capture_event,
)

try:
    from tenacity import RetryError
except ImportError:  # pragma: no cover
    RetryError = Exception  # type: ignore[misc, assignment]

SPRINKLE_VERIFY_TIMEOUT = 60.0
ACTION_PROBE_PREFIXES = (
    "stop_manual",
    "run_program_",
    "sprinkle_station_",
    "stop_after",
    "turn_on_",
    "turn_off_days_",
)
SCHEDULE_WRITE_PROBE_PREFIXES = (
    "set_program_",
    "readback_irrigation_config",
)


def _connection_detail(exc: BaseException) -> str:
    detail = str(exc)
    if isinstance(exc, RetryError):
        detail = "BLE operation failed after retries"
    if "not connected" in detail.lower() or isinstance(exc, RetryError):
        detail = f"{detail}. {BLE_BUSY_HINT}"
    return detail


def _print_step(step: StepResult) -> None:
    mark = "PASS" if step.ok else "FAIL"
    print(f"[{mark}] {step.name}")
    if step.data:
        if "controller_state" in step.data:
            print(f"       {format_status(step.data)}")
        elif "raw_hex" in step.data and "major" in step.data:
            print(f"       firmware={step.data['raw_hex']}")
        elif step.data and all(isinstance(key, int) for key in step.data):
            for key in sorted(step.data):
                print(f"       station {key}: {step.data[key]!r}")
    elif step.detail:
        print(f"       {step.detail}")


def _print_program(program_index: int, program: IrrigationProgram) -> None:
    label = PROGRAM_LABELS[program_index]
    print(f"Program {label} ({program_index})")
    print(f"  name: {program['name']!r}")
    print(
        "  header:"
        f" inter_station={program['inter_station_delay']}s"
        f" budget={program['water_budget']}%"
        f" cycle={program['cycle']}"
        f" week_days=0x{program['week_days']:02x}"
        f" period={program['period_length']}"
        f" synchro_day={program['synchro_day']}"
    )
    start_times = ", ".join(format_minutes(minutes) for minutes in program["start_times"])
    print(f"  start_times: {start_times}")
    durations = ", ".join(
        f"S{station + 1}={seconds}s"
        for station, seconds in enumerate(program["station_durations"])
        if seconds > 0
    )
    print(f"  station_durations: {durations or '(none)'}")


def _print_programs(programs: dict[int, IrrigationProgram]) -> None:
    for program_index in sorted(programs):
        _print_program(program_index, programs[program_index])


def _assemble_station_names(payloads: list[bytes], *, max_stations: int) -> dict[int, str]:
    fragments: dict[int, dict[int, bytes]] = {}
    station_names: dict[int, str] = {}
    for payload in payloads:
        parsed = parse_station_name_fragment(payload)
        if parsed is None or not 1 <= parsed["station"] <= max_stations:
            continue
        station = parsed["station"]
        fragments.setdefault(station, {})[parsed["sequence"]] = parsed["name_bytes"]
        if fragments[station].keys() >= {0, 1}:
            station_fragments = fragments[station]
            station_names[station] = (
                station_fragments[1] + station_fragments[0]
            ).decode("utf-8", errors="replace")
    return station_names


async def _wait_for_watering(
    client: SolemClient,
    *,
    station: int,
    timeout: float,
    verbose: bool,
) -> Mapping[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await client.get_status(include_raw=verbose)
        if status.get("is_watering") and status.get("station_num") == station:
            return status
        await asyncio.sleep(2.0)
    return None


async def _wait_for_program_watering(
    client: SolemClient,
    *,
    program: int,
    timeout: float,
    verbose: bool,
) -> Mapping[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await client.get_status(include_raw=verbose)
        if status.get("is_watering") and status.get("active_program") == program:
            return status
        await asyncio.sleep(2.0)
    return None


def _is_action_capture_probe(probe: str) -> bool:
    return any(probe.startswith(prefix) for prefix in ACTION_PROBE_PREFIXES)


def _is_schedule_write_probe(probe: str) -> bool:
    return any(probe.startswith(prefix) for prefix in SCHEDULE_WRITE_PROBE_PREFIXES)


def _parse_hhmm(value: str) -> int:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("expected HH:MM between 00:00 and 23:59")
    return hour * 60 + minute


async def _run_status_checks(
    client: SolemClient,
    report: list[StepResult],
    *,
    verbose: bool,
    attempts: int = 2,
) -> bool:
    ok = True
    for attempt in range(1, attempts + 1):
        step = StepResult(f"get_status #{attempt}", False)
        try:
            status = await client.get_status(include_raw=verbose)
            step.ok = True
            step.data = status
        except (SolemConnectionError, RetryError) as exc:
            step.detail = _connection_detail(exc)
            ok = False
        report.append(step)
        if not step.ok:
            return False
    return ok


async def _run_firmware_check(
    client: SolemClient,
    report: list[StepResult],
) -> None:
    step = StepResult("get_firmware_version", False)
    try:
        step.data = dict(await client.get_firmware_version())
        step.ok = True
        step.detail = f"firmware={step.data['raw_hex']}"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.append(step)


async def _run_names_check(
    client: SolemClient,
    report: list[StepResult],
) -> None:
    step = StepResult("get_station_names", False)
    try:
        names = await client.get_station_names()
        step.ok = bool(names)
        step.data = names
        step.detail = f"{len(names)} station name(s)"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.append(step)


async def _run_schedule_check(
    client: SolemClient,
    report: list[StepResult],
) -> None:
    step = StepResult("get_irrigation_config", False)
    try:
        programs = await client.get_irrigation_config()
        step.ok = bool(programs)
        step.detail = f"{len(programs)} program(s)"
        report.append(step)
        for program_index in sorted(programs):
            program = programs[program_index]
            detail = (
                f"Program {PROGRAM_LABELS[program_index]}: {program['name']!r}; "
                f"starts="
                + ", ".join(format_minutes(minute) for minute in program["start_times"][:3])
            )
            report.append(
                StepResult(
                    f"schedule program {PROGRAM_LABELS[program_index]}",
                    True,
                    detail=detail,
                )
            )
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
        report.append(step)


async def _run_gatt_check(
    client: SolemClient,
    report: list[StepResult],
) -> None:
    step = StepResult("list_characteristics", False)
    try:
        chars = await client.list_characteristics()
        step.ok = bool(chars)
        step.detail = f"{len(chars)} service(s)"
        report.append(step)
        for service_uuid, characteristics in chars.items():
            print(f"       service {service_uuid}")
            for char in characteristics:
                props = ", ".join(char["properties"])
                print(f"         {char['uuid']} [{props}]")
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
        report.append(step)


async def _run_action_checks(
    client: SolemClient,
    report: list[StepResult],
    *,
    station: int,
    minutes: int,
    run_program: int | None,
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
    report.append(step)

    if skip_sprinkle and run_program is None:
        report.append(
            StepResult("sprinkle test", True, detail="Skipped (--skip-sprinkle)")
        )
        return

    if run_program is not None:
        label = PROGRAM_LABELS[run_program - 1]
        step = StepResult(f"run_program_{label.lower()}", False)
        try:
            await client.run_program_x(run_program)
            step.ok = True
            step.detail = "Command sent"
        except (SolemConnectionError, RetryError) as exc:
            step.detail = _connection_detail(exc)
        report.append(step)
        if not step.ok:
            return

        step = StepResult("verify program watering status", False)
        try:
            status = await _wait_for_program_watering(
                client,
                program=run_program,
                timeout=SPRINKLE_VERIFY_TIMEOUT,
                verbose=verbose,
            )
            if status is None:
                step.detail = (
                    f"Timed out waiting for program {label} "
                    f"(active_program={run_program}) to report watering"
                )
            else:
                step.ok = True
                step.data = status
        except (SolemConnectionError, RetryError) as exc:
            step.detail = _connection_detail(exc)
        report.append(step)

        if step.ok:
            await asyncio.sleep(minutes * 60)

        step = StepResult("stop after program run test", False)
        try:
            await client.stop_manual_sprinkle()
            step.ok = True
            step.detail = "Command sent"
        except (SolemConnectionError, RetryError) as exc:
            step.detail = _connection_detail(exc)
        report.append(step)
        return

    step = StepResult(f"sprinkle_station_{station}_for_{minutes}_minutes", False)
    command_status: Mapping[str, Any] | None = None
    try:
        command_status = await client.sprinkle_station_x_for_y_minutes(station, minutes)
        step.ok = True
        step.detail = "Command sent"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.append(step)
    if not step.ok:
        return

    step = StepResult("verify watering status", False)
    try:
        status = command_status
        if not (
            status
            and status.get("is_watering")
            and status.get("station_num") == station
        ):
            status = await _wait_for_watering(
                client,
                station=station,
                timeout=SPRINKLE_VERIFY_TIMEOUT,
                verbose=verbose,
            )
        if status is None:
            step.detail = f"Timed out waiting for station {station} to report watering"
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
    report.append(step)

    step = StepResult("stop after sprinkle test", False)
    try:
        await client.stop_manual_sprinkle()
        step.ok = True
        step.detail = "Command sent"
    except (SolemConnectionError, RetryError) as exc:
        step.detail = _connection_detail(exc)
    report.append(step)


async def _run_live(args: argparse.Namespace) -> int:
    sections = selected_sections(args.only, include_actions=args.actions)
    try:
        package_version = importlib.metadata.version("solem-blip-ble")
    except importlib.metadata.PackageNotFoundError:
        print("[FAIL] solem-blip-ble is not installed in this Python environment")
        return 1

    client = SolemClient(
        args.mac,
        bluetooth_timeout=args.connect_timeout,
        max_station_num=args.max_stations,
    )
    report: list[StepResult] = []

    print("solem-blip-ble validation")
    print(f"Package:  solem-blip-ble {package_version}")
    print(f"MAC:      {args.mac}")
    print(f"Sections: {', '.join(sections)}")
    if args.run_program is not None:
        action_mode = f"program {PROGRAM_LABELS[args.run_program - 1]}"
    elif args.actions:
        action_mode = "actions"
    else:
        action_mode = "read-only"
    print(f"Mode:     {action_mode}")
    print("-" * 60)

    try:
        if "status" in sections:
            if not await _run_status_checks(client, report, verbose=args.verbose):
                print("-" * 60)
                for step in report:
                    _print_step(step)
                print("-" * 60)
                print("Result: FAILED (status checks did not complete)")
                return 1

        if "firmware" in sections:
            await _run_firmware_check(client, report)
        if "names" in sections:
            await _run_names_check(client, report)
        if "schedule" in sections:
            await _run_schedule_check(client, report)
        if "gatt" in sections:
            await _run_gatt_check(client, report)
        if "actions" in sections and args.actions:
            await _run_action_checks(
                client,
                report,
                station=args.station,
                minutes=args.minutes,
                run_program=args.run_program,
                skip_sprinkle=args.skip_sprinkle,
                verbose=args.verbose,
            )

        print("-" * 60)
        for step in report:
            _print_step(step)
        print("-" * 60)
        if all(step.ok for step in report):
            print("Result: ALL CHECKS PASSED")
            return 0
        failed = [step.name for step in report if not step.ok]
        print(f"Result: FAILED ({len(failed)} step(s): {', '.join(failed)})")
        return 1
    finally:
        await client.disconnect()


async def _capture(args: argparse.Namespace) -> int:
    sections = selected_sections(args.only, include_actions=False)
    probes = capture_probes(sections)
    if not probes:
        print("No capture probes selected.", file=sys.stderr)
        return 1

    output_path = (
        args.capture
        if isinstance(args.capture, Path)
        else default_capture_output(args.capture_prefix)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    active_probe = "setup"
    irrigation_first_fragments: dict[int, int] = {}

    with output_path.open("w", encoding="utf-8") as output:
        def record(direction: str, probe: str, payload: bytes, note: str = "") -> None:
            write_capture_event(
                output,
                CaptureEvent(
                    timestamp=timestamp(),
                    elapsed_seconds=round(monotonic() - started, 6),
                    direction=direction,
                    probe=probe,
                    payload_hex=payload.hex(),
                    note=note,
                ),
                verbose=args.verbose,
            )

        def notification_handler(_sender: Any, data: bytearray) -> None:
            payload = bytes(data)
            record(
                "RX",
                active_probe,
                payload,
                describe_notification(
                    payload,
                    probe=active_probe,
                    irrigation_first_fragments=irrigation_first_fragments,
                ),
            )

        print(f"Scanning for {args.mac}...")
        device = await BleakScanner.find_device_by_address(
            args.mac, timeout=args.scan_timeout
        )
        if device is None:
            print(f"Device not found: {args.mac}", file=sys.stderr)
            return 1

        print(f"Connecting to {args.mac}...")
        client = SolemClient(
            args.mac,
            bluetooth_timeout=args.connect_timeout,
            ble_device=device,
        )
        async with client.raw_ble_session() as session:
            ble_client = session.client
            notify_char = session.notify_characteristic
            write_char = session.write_characteristic
            await ble_client.start_notify(notify_char, notification_handler)
            await asyncio.sleep(args.settle_seconds)

            for probe_name, payload in probes:
                active_probe = probe_name
                dwell = (
                    args.schedule_capture_seconds
                    if probe_name == "irrigation_config"
                    else args.capture_seconds
                )
                print(f"Probe {probe_name}: {payload.hex()}")
                record("TX", probe_name, payload)
                await ble_client.write_gatt_char(write_char, payload, response=False)
                await asyncio.sleep(dwell)

            active_probe = "teardown"
            await ble_client.stop_notify(notify_char)

    print(f"Capture written to {output_path}")
    return 0


async def _capture_actions(args: argparse.Namespace) -> int:
    try:
        writes = action_capture_writes(
            run_program=args.run_program,
            station=args.station,
            minutes=args.minutes,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_path = (
        args.capture
        if isinstance(args.capture, Path)
        else default_capture_output(args.capture_prefix)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    active_probe = "setup"
    irrigation_first_fragments: dict[int, int] = {}

    if args.run_program is not None:
        label = PROGRAM_LABELS[args.run_program - 1]
        action_label = f"program {label}"
    else:
        action_label = f"station {args.station} for {args.minutes} min"

    with output_path.open("w", encoding="utf-8") as output:
        def record(direction: str, probe: str, payload: bytes, note: str = "") -> None:
            write_capture_event(
                output,
                CaptureEvent(
                    timestamp=timestamp(),
                    elapsed_seconds=round(monotonic() - started, 6),
                    direction=direction,
                    probe=probe,
                    payload_hex=payload.hex(),
                    note=note,
                ),
                verbose=args.verbose,
            )

        def notification_handler(_sender: Any, data: bytearray) -> None:
            payload = bytes(data)
            record(
                "RX",
                active_probe,
                payload,
                describe_notification(
                    payload,
                    probe=active_probe,
                    irrigation_first_fragments=irrigation_first_fragments,
                ),
            )

        print(f"Scanning for {args.mac}...")
        device = await BleakScanner.find_device_by_address(
            args.mac, timeout=args.scan_timeout
        )
        if device is None:
            print(f"Device not found: {args.mac}", file=sys.stderr)
            return 1

        print(f"Connecting to {args.mac}...")
        print(f"Action capture: {action_label}")
        client = SolemClient(
            args.mac,
            bluetooth_timeout=args.connect_timeout,
            ble_device=device,
        )
        async with client.raw_ble_session() as session:
            ble_client = session.client
            notify_char = session.notify_characteristic
            write_char = session.write_characteristic
            await ble_client.start_notify(notify_char, notification_handler)
            await asyncio.sleep(args.settle_seconds)

            for probe_name, payload in writes:
                active_probe = probe_name
                dwell = action_listen_dwell_seconds(
                    probe_name,
                    minutes=args.minutes,
                    capture_seconds=args.capture_seconds,
                )
                print(f"Write {probe_name}: {payload.hex()} (listen {dwell:.0f}s)")
                record("TX", probe_name, payload)
                await ble_client.write_gatt_char(write_char, payload, response=False)
                await asyncio.sleep(dwell)

            active_probe = "teardown"
            await ble_client.stop_notify(notify_char)

    print(f"Action capture written to {output_path}")
    return 0


async def _capture_off_days(args: argparse.Namespace) -> int:
    try:
        writes = off_days_capture_writes(args.capture_off_days)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_path = (
        args.capture
        if isinstance(args.capture, Path)
        else default_capture_output(args.capture_prefix)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    active_probe = "setup"
    irrigation_first_fragments: dict[int, int] = {}

    with output_path.open("w", encoding="utf-8") as output:
        def record(direction: str, probe: str, payload: bytes, note: str = "") -> None:
            write_capture_event(
                output,
                CaptureEvent(
                    timestamp=timestamp(),
                    elapsed_seconds=round(monotonic() - started, 6),
                    direction=direction,
                    probe=probe,
                    payload_hex=payload.hex(),
                    note=note,
                ),
                verbose=args.verbose,
            )

        def notification_handler(_sender: Any, data: bytearray) -> None:
            payload = bytes(data)
            record(
                "RX",
                active_probe,
                payload,
                describe_notification(
                    payload,
                    probe=active_probe,
                    irrigation_first_fragments=irrigation_first_fragments,
                ),
            )

        print(f"Scanning for {args.mac}...")
        device = await BleakScanner.find_device_by_address(
            args.mac, timeout=args.scan_timeout
        )
        if device is None:
            print(f"Device not found: {args.mac}", file=sys.stderr)
            return 1

        print(f"Connecting to {args.mac}...")
        print(f"Off-days capture: {args.capture_off_days} day(s)")
        client = SolemClient(
            args.mac,
            bluetooth_timeout=args.connect_timeout,
            ble_device=device,
        )
        async with client.raw_ble_session() as session:
            ble_client = session.client
            notify_char = session.notify_characteristic
            write_char = session.write_characteristic
            await ble_client.start_notify(notify_char, notification_handler)
            await asyncio.sleep(args.settle_seconds)

            for probe_name, payload in writes:
                active_probe = probe_name
                print(
                    f"Write {probe_name}: {payload.hex()} "
                    f"(listen {args.capture_seconds:.0f}s)"
                )
                record("TX", probe_name, payload)
                await ble_client.write_gatt_char(write_char, payload, response=False)
                await asyncio.sleep(args.capture_seconds)

            active_probe = "teardown"
            await ble_client.stop_notify(notify_char)

    print(f"Off-days capture written to {output_path}")
    return 0


async def _capture_schedule_write(args: argparse.Namespace) -> int:
    try:
        expected, writes = schedule_write_capture_writes(
            program_index=args.write_schedule_program - 1,
            program_name=args.write_schedule_name,
            station=args.write_schedule_station,
            duration_seconds=args.write_schedule_duration,
            start_minutes=args.write_schedule_start,
            max_stations=args.max_stations,
            today=date.today(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output_path = (
        args.capture
        if isinstance(args.capture, Path)
        else default_capture_output(args.capture_prefix)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    active_probe = "setup"
    irrigation_first_fragments: dict[int, int] = {}
    readback_payloads: list[bytes] = []

    label = PROGRAM_LABELS[args.write_schedule_program - 1]
    write_frames = [
        (probe_name, payload)
        for probe_name, payload in writes
        if probe_name != "readback_irrigation_config"
    ]
    readback_frame = next(
        payload
        for probe_name, payload in writes
        if probe_name == "readback_irrigation_config"
    )

    with output_path.open("w", encoding="utf-8") as output:
        def record(direction: str, probe: str, payload: bytes, note: str = "") -> None:
            write_capture_event(
                output,
                CaptureEvent(
                    timestamp=timestamp(),
                    elapsed_seconds=round(monotonic() - started, 6),
                    direction=direction,
                    probe=probe,
                    payload_hex=payload.hex(),
                    note=note,
                ),
                verbose=args.verbose,
            )

        def notification_handler(_sender: Any, data: bytearray) -> None:
            payload = bytes(data)
            if active_probe == "readback_irrigation_config":
                readback_payloads.append(payload)
            record(
                "RX",
                active_probe,
                payload,
                describe_notification(
                    payload,
                    probe=active_probe,
                    irrigation_first_fragments=irrigation_first_fragments,
                ),
            )

        async def write_frame_with_retry(
            probe_name: str,
            payload: bytes,
            *,
            dwell: float,
        ) -> bool:
            nonlocal active_probe
            for attempt in range(1, args.write_schedule_frame_attempts + 1):
                active_probe = probe_name
                if attempt == 1:
                    print(
                        f"Write {probe_name}: {payload.hex()} "
                        f"(listen {dwell:.0f}s)"
                    )
                else:
                    print(
                        f"Retry {probe_name} attempt {attempt}: {payload.hex()} "
                        f"(listen {dwell:.0f}s)"
                    )

                try:
                    device = await BleakScanner.find_device_by_address(
                        args.mac, timeout=args.scan_timeout
                    )
                    if device is None:
                        raise SolemConnectionError(f"Device not found: {args.mac}")

                    client = SolemClient(
                        args.mac,
                        bluetooth_timeout=args.connect_timeout,
                        ble_device=device,
                    )
                    async with client.raw_ble_session() as session:
                        ble_client = session.client
                        notify_char = session.notify_characteristic
                        write_char = session.write_characteristic
                        await ble_client.start_notify(
                            notify_char,
                            notification_handler,
                        )
                        await asyncio.sleep(args.settle_seconds)
                        await ble_client.write_gatt_char(
                            write_char,
                            payload,
                            response=False,
                        )
                        record("TX", probe_name, payload)
                        await asyncio.sleep(dwell)
                        active_probe = "teardown"
                        await ble_client.stop_notify(notify_char)
                    return True
                except (BleakError, OSError, TimeoutError, SolemConnectionError) as exc:
                    if attempt >= args.write_schedule_frame_attempts:
                        print(
                            f"Write {probe_name} failed after {attempt} attempt(s): "
                            f"{exc}",
                            file=sys.stderr,
                        )
                        return False
                    print(
                        f"Write {probe_name} attempt {attempt} failed: {exc}; "
                        f"retrying after {args.write_schedule_reconnect_seconds:.1f}s",
                        file=sys.stderr,
                    )
                    active_probe = "retry_wait"
                    await asyncio.sleep(args.write_schedule_reconnect_seconds)
            return False

        print(
            f"Schedule write capture: program {label}, "
            f"station {args.write_schedule_station}, "
            f"duration {args.write_schedule_duration}s"
        )
        for probe_name, payload in write_frames:
            if not await write_frame_with_retry(
                probe_name,
                payload,
                dwell=args.capture_seconds,
            ):
                print(f"Schedule write capture written to {output_path}")
                return 1

        print(
            "Reconnecting for read-back "
            f"(wait {args.write_schedule_reconnect_seconds:.0f}s)..."
        )
        await asyncio.sleep(args.write_schedule_reconnect_seconds)
        if not await write_frame_with_retry(
            "readback_irrigation_config",
            readback_frame,
            dwell=args.schedule_capture_seconds,
        ):
            print(f"Schedule write capture written to {output_path}")
            return 1

    programs = assemble_irrigation_programs(
        readback_payloads,
        max_stations=args.max_stations,
    )
    actual = programs.get(args.write_schedule_program - 1)
    print(f"Schedule write capture written to {output_path}")
    if actual == expected:
        print("Read-back verification: PASS")
        _print_program(args.write_schedule_program - 1, actual)
        return 0

    print("Read-back verification: FAIL")
    print("Expected:")
    _print_program(args.write_schedule_program - 1, expected)
    if actual is None:
        print(f"Program {label} was not present in read-back capture")
    else:
        print("Actual:")
        _print_program(args.write_schedule_program - 1, actual)
    return 1


def _replay_status(events: list[dict[str, Any]], *, max_stations: int, verbose: bool) -> bool:
    payloads = [bytes.fromhex(event["payload_hex"]) for event in events]
    ok = False
    for index, payload in enumerate(payloads, start=1):
        parsed = parse_status_notification(payload, max_station_num=max_stations)
        if parsed is None:
            if verbose:
                print(f"status #{index}: unparsed {payload.hex()}")
            continue
        ok = True
        print(f"status #{index}: {format_status(parsed)}")
        if verbose:
            print(f"  raw={payload.hex()}")
    return ok


def _replay_firmware(events: list[dict[str, Any]], *, verbose: bool) -> bool:
    ok = False
    for index, event in enumerate(events, start=1):
        payload = bytes.fromhex(event["payload_hex"])
        parsed = parse_firmware_version_response(payload)
        if parsed is None:
            if verbose:
                print(f"firmware #{index}: unparsed {payload.hex()}")
            continue
        ok = True
        print(f"firmware #{index}: {parsed['raw_hex']}")
        if verbose:
            print(f"  raw={payload.hex()}")
    return ok


def _replay_names(events: list[dict[str, Any]], *, max_stations: int, verbose: bool) -> bool:
    payloads = [bytes.fromhex(event["payload_hex"]) for event in events]
    names = _assemble_station_names(payloads, max_stations=max_stations)
    if verbose:
        for payload in payloads:
            print(f"  raw={payload.hex()} | {describe_notification(payload)}")
    if not names:
        return False
    for station in sorted(names):
        print(f"station {station}: {names[station]!r}")
    return True


def _replay_schedule(events: list[dict[str, Any]], *, max_stations: int, verbose: bool) -> bool:
    payloads = [bytes.fromhex(event["payload_hex"]) for event in events]
    if verbose:
        first_fragments: dict[int, int] = {}
        for payload in payloads:
            print(
                f"  raw={payload.hex()} | "
                f"{describe_notification(payload, irrigation_first_fragments=first_fragments)}"
            )
    programs = assemble_irrigation_programs(payloads, max_stations=max_stations)
    if not programs:
        return False
    _print_programs(programs)
    return irrigation_config_complete(payloads)


def _replay_actions(events: list[dict[str, Any]], *, max_stations: int, verbose: bool) -> bool:
    ok = False
    rx_index = 0
    for event in events:
        if event.get("direction") != "RX":
            if verbose and event.get("direction") == "TX":
                probe = event.get("probe", "")
                if _is_action_capture_probe(probe):
                    print(
                        f"tx {probe}: {event.get('payload_hex', '')} "
                        f"@ {event.get('elapsed_seconds', 0):.3f}s"
                    )
            continue
        probe = event.get("probe", "")
        if not _is_action_capture_probe(probe):
            continue
        payload = bytes.fromhex(event["payload_hex"])
        parsed = parse_status_notification(payload, max_station_num=max_stations)
        rx_index += 1
        if parsed is None:
            note = describe_notification(payload, probe=probe)
            if verbose or "status" not in note:
                print(f"actions #{rx_index} [{probe}]: {note}")
                if verbose:
                    print(f"  raw={payload.hex()}")
            continue
        ok = True
        print(f"actions #{rx_index} [{probe}]: {format_status(parsed)}")
        if verbose:
            print(f"  raw={payload.hex()}")
    return ok


def _replay_schedule_write(
    events: list[dict[str, Any]], *, max_stations: int, verbose: bool
) -> bool:
    ok = False
    readback_payloads: list[bytes] = []
    first_fragments: dict[int, int] = {}
    for event in events:
        probe = event.get("probe", "")
        if not _is_schedule_write_probe(probe):
            continue
        direction = event.get("direction")
        payload = bytes.fromhex(event["payload_hex"])
        if direction == "TX":
            print(
                f"tx {probe}: {event.get('payload_hex', '')} "
                f"@ {event.get('elapsed_seconds', 0):.3f}s"
            )
            continue
        if direction != "RX":
            continue
        if probe == "readback_irrigation_config":
            readback_payloads.append(payload)
        note = describe_notification(
            payload,
            probe=probe,
            irrigation_first_fragments=first_fragments,
        )
        if verbose:
            print(f"rx {probe}: {payload.hex()} | {note}")

    if readback_payloads:
        programs = assemble_irrigation_programs(
            readback_payloads,
            max_stations=max_stations,
        )
        if programs:
            _print_programs(programs)
            ok = irrigation_config_complete(readback_payloads)
    return ok


def _replay(args: argparse.Namespace) -> int:
    include_actions = any(
        section in (args.only or []) for section in ("actions", "schedule_write")
    )
    sections = selected_sections(args.only, include_actions=include_actions)
    events, used_paths = load_capture_events(args.replay, direction=None)
    if not events:
        paths = ", ".join(str(path) for path in args.replay)
        print(f"No capture events found in: {paths}", file=sys.stderr)
        return 1

    source = used_paths[0] if len(used_paths) == 1 else f"{len(used_paths)} files"
    print(f"Replay from {source} ({len(events)} event(s))")
    print("-" * 60)

    section_ok: dict[str, bool] = {}
    if "status" in sections:
        status_events = [event for event in events if event.get("probe") == "status"]
        print("[status]")
        section_ok["status"] = _replay_status(
            status_events, max_stations=args.max_stations, verbose=args.verbose
        )
        print()

    if "firmware" in sections:
        firmware_events = [event for event in events if event.get("probe") == "firmware"]
        print("[firmware]")
        section_ok["firmware"] = _replay_firmware(firmware_events, verbose=args.verbose)
        print()

    if "names" in sections:
        name_events = [event for event in events if event.get("probe") == "output_names"]
        print("[names]")
        section_ok["names"] = _replay_names(
            name_events, max_stations=args.max_stations, verbose=args.verbose
        )
        print()

    if "schedule" in sections:
        schedule_events = [
            event for event in events if event.get("probe") == "irrigation_config"
        ]
        print("[schedule]")
        section_ok["schedule"] = _replay_schedule(
            schedule_events, max_stations=args.max_stations, verbose=args.verbose
        )
        print()

    if "actions" in sections:
        print("[actions]")
        section_ok["actions"] = _replay_actions(
            events, max_stations=args.max_stations, verbose=args.verbose
        )
        print()

    if "schedule_write" in sections:
        print("[schedule_write]")
        section_ok["schedule_write"] = _replay_schedule_write(
            events, max_stations=args.max_stations, verbose=args.verbose
        )
        print()

    print("-" * 60)
    failed = [name for name, ok in section_ok.items() if not ok]
    if failed:
        print(f"Result: PARTIAL (failed sections: {', '.join(failed)})")
        return 1
    print("Result: ALL SELECTED SECTIONS DECODED")
    return 0


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Validate and troubleshoot Solem BL-IP BLE library features",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sections (--only):
  status     Poll controller state via 3b00 commit
  firmware   Read identification / firmware version (0f00)
  names      Read station output names (3500)
  schedule   Read persisted irrigation programs (3900)
  gatt       List GATT services and characteristics
  actions    Manual control writes (requires --actions)
  schedule_write   Persist one test schedule, then read back

Capture modes:
  --capture              Read probes only (status, firmware, names, schedule)
  --capture --actions    Action writes + notification capture (default: station 1, 1 min)
  --capture --actions --run-program 1   Program A run for --minutes, then stop
  --capture --write-schedule 2          Write a small Program B schedule, then read back
  --capture-off-days 3   Turn off for 3 days, capture status, then turn back on

Examples:
  validate-solem-blip AA:BB:CC:DD:EE:FF
  validate-solem-blip AA:BB:CC:DD:EE:FF --verbose
  validate-solem-blip AA:BB:CC:DD:EE:FF --only status --only firmware
  validate-solem-blip AA:BB:CC:DD:EE:FF --capture --verbose
  validate-solem-blip AA:BB:CC:DD:EE:FF --capture --actions --run-program 1 --minutes 1
  validate-solem-blip AA:BB:CC:DD:EE:FF --capture --write-schedule 2 --write-schedule-station 5
  validate-solem-blip AA:BB:CC:DD:EE:FF --capture-off-days 3 --verbose
  validate-solem-blip AA:BB:CC:DD:EE:FF --replay capture.jsonl --only schedule_write
  validate-solem-blip AA:BB:CC:DD:EE:FF --replay capture.jsonl --only actions
  validate-solem-blip AA:BB:CC:DD:EE:FF --actions --run-program 1 --minutes 1
""".strip(),
    )
    parser.add_argument(
        "mac",
        help="Controller Bluetooth MAC address (required)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--capture",
        nargs="?",
        const="auto",
        metavar="PATH",
        help="Capture raw read-probe notifications to JSONL",
    )
    mode.add_argument(
        "--replay",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="Replay and decode one or more JSONL captures offline",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=ALL_SECTIONS,
        dest="only",
        help="Limit validation to specific section(s); repeatable",
    )
    parser.add_argument(
        "--actions",
        action="store_true",
        help="Include manual write/action checks (stop, sprinkle or program run)",
    )
    parser.add_argument(
        "--capture-off-days",
        type=int,
        choices=range(1, 16),
        metavar="N",
        help="Capture turn-on, temporary off for N days, and turn-on recovery",
    )
    parser.add_argument(
        "--write-schedule",
        type=int,
        choices=range(1, len(PROGRAM_LABELS) + 1),
        metavar="N",
        dest="write_schedule_program",
        help="With --capture, write program N (1=A, 2=B, 3=C), then read back",
    )
    parser.add_argument(
        "--write-schedule-name",
        default="Codex Test",
        help="Program name for --write-schedule (default: Codex Test)",
    )
    parser.add_argument(
        "--write-schedule-start",
        type=_parse_hhmm,
        default=None,
        metavar="HH:MM",
        help="Optional start time for --write-schedule; omitted disables all starts",
    )
    parser.add_argument(
        "--write-schedule-station",
        type=int,
        default=1,
        help="Station enabled by --write-schedule (default: 1)",
    )
    parser.add_argument(
        "--write-schedule-duration",
        type=int,
        default=60,
        help="Station duration in seconds for --write-schedule (default: 60)",
    )
    parser.add_argument(
        "--write-schedule-reconnect-seconds",
        type=float,
        default=3.0,
        help="Delay before reconnecting for --write-schedule read-back (default: 3)",
    )
    parser.add_argument(
        "--write-schedule-frame-attempts",
        type=int,
        default=3,
        help="Attempts per --write-schedule frame after BLE reconnects (default: 3)",
    )
    parser.add_argument(
        "--run-program",
        type=int,
        choices=range(1, len(PROGRAM_LABELS) + 1),
        metavar="N",
        help="Run program N (1=A, 2=B, 3=C) instead of manual station sprinkle",
    )
    parser.add_argument(
        "--max-stations",
        type=int,
        default=6,
        help="Configured station count (default: 6)",
    )
    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=DEFAULT_CAPTURE_SECONDS,
        help=f"Capture dwell per probe except schedule (default: {DEFAULT_CAPTURE_SECONDS})",
    )
    parser.add_argument(
        "--schedule-capture-seconds",
        type=float,
        default=DEFAULT_SCHEDULE_CAPTURE_SECONDS,
        help=(
            "Capture dwell for irrigation_config probe "
            f"(default: {DEFAULT_SCHEDULE_CAPTURE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--capture-prefix",
        default="solem-validate",
        help="Output filename prefix for --capture (default: solem-validate)",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help=f"Delay after enabling notifications (default: {DEFAULT_SETTLE_SECONDS})",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=10.0,
        help="BLE scan timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=30.0,
        help="BLE connection timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--station",
        type=int,
        default=1,
        help="Station for sprinkle action test (default: 1)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=1,
        help="Minutes for sprinkle action test (default: 1)",
    )
    parser.add_argument(
        "--skip-sprinkle",
        action="store_true",
        help="With --actions, only run stop (no sprinkle test)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include raw notification hex where supported",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    args.mac = args.mac.upper()
    if args.run_program is not None and not args.actions:
        print("--run-program requires --actions", file=sys.stderr)
        return 2
    if args.write_schedule_program is not None and not args.capture:
        print("--write-schedule requires --capture", file=sys.stderr)
        return 2
    if args.write_schedule_program is not None and args.actions:
        print("--write-schedule cannot be combined with --actions", file=sys.stderr)
        return 2
    if args.write_schedule_program is not None and args.capture_off_days is not None:
        print("--write-schedule cannot be combined with --capture-off-days", file=sys.stderr)
        return 2
    if args.write_schedule_program is not None and args.replay:
        print("--write-schedule cannot be combined with --replay", file=sys.stderr)
        return 2
    if args.capture_off_days is not None and args.replay:
        print("--capture-off-days cannot be used with --replay", file=sys.stderr)
        return 2
    if args.capture_off_days is not None and args.actions:
        print("--capture-off-days cannot be combined with --actions", file=sys.stderr)
        return 2
    if args.capture_off_days is not None:
        if args.capture == "auto" or args.capture is None:
            args.capture = default_capture_output(args.capture_prefix)
        else:
            args.capture = Path(args.capture)
        return await _capture_off_days(args)
    if args.capture:
        if args.capture == "auto":
            args.capture = default_capture_output(args.capture_prefix)
        else:
            args.capture = Path(args.capture)
        if args.actions:
            return await _capture_actions(args)
        if args.write_schedule_program is not None:
            return await _capture_schedule_write(args)
        return await _capture(args)
    if args.replay:
        return _replay(args)
    return await _run_live(args)


def main(default_only: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args()
    if default_only and not args.only:
        args.only = list(default_only)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("Validation interrupted", file=sys.stderr)
        return 130
    except RetryError as exc:
        print(f"[FAIL] {_connection_detail(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
