"""Validate Solem BL-IP V5 irrigation schedule read support.

Installed entry point: ``validate-solem-schedule-read``.

Examples:
  validate-solem-schedule-read --live
  validate-solem-schedule-read --capture --verbose
  validate-solem-schedule-read --replay btsnoop/captures/solem-schedule-*.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from bleak import BleakClient, BleakScanner

from solem_blip_ble import (
    IrrigationProgram,
    SolemClient,
    SolemConnectionError,
    assemble_irrigation_programs,
    irrigation_config_complete,
    normalize_config_notification,
    pack_get_irrigation_config,
)

try:
    from tenacity import RetryError
except ImportError:  # pragma: no cover
    RetryError = Exception  # type: ignore[misc, assignment]

DEFAULT_MAC = "C8:B9:61:D4:4D:C8"
WRITE_CHAR_UUID = "108b0002-eab5-bc09-d0ea-0b8f467ce8ee"
NOTIFY_CHAR_UUID = "108b0003-eab5-bc09-d0ea-0b8f467ce8ee"
DEFAULT_SETTLE_SECONDS = 0.5
DEFAULT_CAPTURE_SECONDS = 15.0
PROGRAM_LABELS = ("A", "B", "C")
IRRIGATION_CHUNK_NAMES = {
    0: "name_part_1",
    1: "name_part_2",
    2: "header",
    3: "start_times",
    4: "durations_1_5",
    5: "durations_6_10",
    6: "durations_11_12",
}


@dataclass
class CaptureEvent:
    timestamp: str
    elapsed_seconds: float
    direction: str
    probe: str
    payload_hex: str
    note: str = ""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _default_capture_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / "btsnoop" / "captures" / f"solem-schedule-{stamp}.jsonl"


def _format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "disabled"
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def _describe_fragment(
    payload: bytes,
    *,
    first_fragments: dict[int, int] | None = None,
) -> str:
    normalized = normalize_config_notification(payload)
    if normalized is None:
        return ""
    program_class = normalized[3] >> 4
    program_index = normalized[3] & 0x0F
    fragment_id = normalized[2]
    if program_class == 1 and first_fragments is not None:
        first_fragment_id = max(first_fragments.get(program_index, fragment_id), fragment_id)
        first_fragments[program_index] = first_fragment_id
        logical_chunk = first_fragment_id - fragment_id
        chunk_label = IRRIGATION_CHUNK_NAMES.get(
            logical_chunk, f"chunk_{logical_chunk}"
        )
        return (
            f"irrigation program={program_index} fragment={fragment_id} "
            f"{chunk_label}"
        )
    return (
        f"class={program_class} program={program_index} fragment={fragment_id}"
    )


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
    )
    start_times = ", ".join(
        _format_minutes(minutes) for minutes in program["start_times"]
    )
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


def _load_capture_payloads(
    paths: list[Path],
    *,
    probe: str = "irrigation_config",
) -> tuple[list[bytes], list[Path]]:
    payloads: list[bytes] = []
    used_paths: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        file_payloads: list[bytes] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("direction") != "RX" or event.get("probe") != probe:
                continue
            file_payloads.append(bytes.fromhex(event["payload_hex"]))
        if file_payloads:
            payloads.extend(file_payloads)
            used_paths.append(path)
    return payloads, used_paths


def _write_event(output, event: CaptureEvent, *, verbose: bool) -> None:
    output.write(json.dumps(asdict(event), sort_keys=True) + "\n")
    output.flush()
    if verbose or event.direction == "TX":
        suffix = f" | {event.note}" if event.note else ""
        print(
            f"{event.elapsed_seconds:8.3f}s {event.direction} "
            f"{event.probe:16s} {event.payload_hex}{suffix}"
        )


async def _capture(args: argparse.Namespace) -> int:
    output_path: Path = args.capture if isinstance(args.capture, Path) else _default_capture_output()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    active_probe = "setup"
    first_fragments: dict[int, int] = {}

    with output_path.open("w", encoding="utf-8") as output:
        def record(direction: str, probe: str, payload: bytes, note: str = "") -> None:
            _write_event(
                output,
                CaptureEvent(
                    timestamp=_timestamp(),
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
                _describe_fragment(payload, first_fragments=first_fragments),
            )

        print(f"Scanning for {args.mac}...")
        device = await BleakScanner.find_device_by_address(
            args.mac, timeout=args.scan_timeout
        )
        if device is None:
            print(f"Device not found: {args.mac}", file=sys.stderr)
            return 1

        print(f"Connecting to {args.mac}...")
        async with BleakClient(device, timeout=args.connect_timeout) as client:
            await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
            await asyncio.sleep(args.settle_seconds)

            active_probe = "irrigation_config"
            request = pack_get_irrigation_config()
            print(f"Probe irrigation_config: {request.hex()}")
            record("TX", active_probe, request)
            await client.write_gatt_char(
                WRITE_CHAR_UUID, request, response=False
            )
            await asyncio.sleep(args.capture_seconds)

            active_probe = "teardown"
            await client.stop_notify(NOTIFY_CHAR_UUID)

    print(f"Capture written to {output_path}")
    return 0


async def _live(args: argparse.Namespace) -> int:
    client = SolemClient(args.mac, max_station_num=args.max_stations)
    try:
        programs = await client.get_irrigation_config()
    except (SolemConnectionError, RetryError) as exc:
        print(f"Live read failed: {exc}", file=sys.stderr)
        return 1

    print(f"Read {len(programs)} irrigation program(s) from {args.mac}")
    _print_programs(programs)
    return 0


def _replay(args: argparse.Namespace) -> int:
    payloads, used_paths = _load_capture_payloads(args.replay)
    if not payloads:
        paths = ", ".join(str(path) for path in args.replay)
        print(
            f"No irrigation_config RX payloads found in: {paths}. "
            "Pass the capture file explicitly, e.g. "
            "`validate-solem-schedule-read --replay btsnoop/captures/solem-schedule-20260531-224603.jsonl`",
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        first_fragments: dict[int, int] = {}
        for payload in payloads:
            print(
                f"{payload.hex()} | "
                f"{_describe_fragment(payload, first_fragments=first_fragments)}"
            )

    programs = assemble_irrigation_programs(payloads, max_stations=args.max_stations)
    complete = irrigation_config_complete(payloads)
    source = used_paths[0] if len(used_paths) == 1 else f"{len(used_paths)} files"
    print(
        f"Replayed {len(payloads)} notification(s) from {source} "
        f"({'complete' if complete else 'partial'})"
    )
    _print_programs(programs)
    return 0 if complete else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Solem BL-IP V5 irrigation schedule read support"
    )
    parser.add_argument(
        "mac",
        nargs="?",
        default=os.environ.get("SOLEM_MAC", DEFAULT_MAC),
        help=f"Controller MAC (default: {DEFAULT_MAC} or SOLEM_MAC env)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Read schedule from device via SolemClient (default mode)",
    )
    mode.add_argument(
        "--capture",
        nargs="?",
        const="auto",
        metavar="PATH",
        help="Capture raw 3900 notifications to JSONL",
    )
    mode.add_argument(
        "--replay",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="Replay and decode one or more JSONL captures offline",
    )
    parser.add_argument(
        "--max-stations",
        type=int,
        default=12,
        help="Maximum station count for duration parsing (default: 12)",
    )
    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=DEFAULT_CAPTURE_SECONDS,
        help=f"Seconds to capture after 3900 (default: {DEFAULT_CAPTURE_SECONDS})",
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
        "--verbose",
        action="store_true",
        help="Print raw notifications and fragment classification",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    args.mac = args.mac.upper()
    if args.capture:
        if args.capture == "auto":
            args.capture = _default_capture_output()
        else:
            args.capture = Path(args.capture)
        return await _capture(args)
    if args.replay:
        return _replay(args)
    return await _live(args)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("Validation interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
