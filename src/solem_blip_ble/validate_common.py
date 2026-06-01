"""Shared helpers for Solem BL-IP validation and capture tooling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from solem_blip_ble import protocol

WRITE_CHAR_UUID = "108b0002-eab5-bc09-d0ea-0b8f467ce8ee"
NOTIFY_CHAR_UUID = "108b0003-eab5-bc09-d0ea-0b8f467ce8ee"
DEFAULT_SETTLE_SECONDS = 0.5
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_SCHEDULE_CAPTURE_SECONDS = 15.0

READ_SECTIONS = ("status", "firmware", "names", "schedule", "gatt")
ACTION_SECTIONS = ("actions",)
ALL_SECTIONS = READ_SECTIONS + ACTION_SECTIONS
PROGRAM_LABELS = ("A", "B", "C")
DEFAULT_ACTION_LISTEN_BUFFER_SECONDS = 5.0

IRRIGATION_CHUNK_NAMES = {
    0: "name_part_1",
    1: "name_part_2",
    2: "header",
    3: "start_times",
    4: "durations_1_5",
    5: "durations_6_10",
    6: "durations_11_12",
}

BLE_BUSY_HINT = (
    "Hint: stop Home Assistant / other BLE clients using this controller, "
    "then retry."
)


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    data: Mapping[Any, Any] | None = None


@dataclass
class CaptureEvent:
    timestamp: str
    elapsed_seconds: float
    direction: str
    probe: str
    payload_hex: str
    note: str = ""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_capture_output(prefix: str = "solem-validate") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / "btsnoop" / "captures" / f"{prefix}-{stamp}.jsonl"


def selected_sections(
    only: list[str] | None,
    *,
    include_actions: bool = False,
) -> tuple[str, ...]:
    if only:
        allowed = set(ALL_SECTIONS if include_actions else READ_SECTIONS)
        sections = tuple(section for section in only if section in allowed)
        return sections or tuple(READ_SECTIONS)
    if include_actions:
        return ALL_SECTIONS
    return READ_SECTIONS


def action_capture_writes(
    *,
    run_program: int | None = None,
    station: int = 1,
    minutes: int = 1,
) -> list[tuple[str, bytes]]:
    """BLE writes for action capture: cleanup, start watering, final stop."""
    if run_program is not None:
        if not 1 <= run_program <= len(PROGRAM_LABELS):
            raise ValueError(f"run_program must be between 1 and {len(PROGRAM_LABELS)}")
        start_label = f"run_program_{PROGRAM_LABELS[run_program - 1].lower()}"
        start_command = protocol.pack_run_program(run_program)
    else:
        if not 1 <= station <= protocol.MAX_STATION_NUM:
            raise ValueError(
                f"station must be between 1 and {protocol.MAX_STATION_NUM}"
            )
        if not 1 <= minutes <= 240:
            raise ValueError("minutes must be between 1 and 240")
        start_label = f"sprinkle_station_{station}"
        start_command = protocol.pack_sprinkle_station(station, minutes)

    return [
        ("stop_manual_command", protocol.pack_stop_manual_sprinkle()),
        ("stop_manual_commit", protocol.pack_commit()),
        (f"{start_label}_command", start_command),
        (f"{start_label}_commit", protocol.pack_commit()),
        ("stop_after_command", protocol.pack_stop_manual_sprinkle()),
        ("stop_after_commit", protocol.pack_commit()),
    ]


def action_listen_dwell_seconds(
    probe_name: str,
    *,
    minutes: int,
    capture_seconds: float,
) -> float:
    """Return how long to record notifications after a start commit."""
    if probe_name.endswith("_commit") and (
        probe_name.startswith("run_program_") or probe_name.startswith("sprinkle_station_")
    ):
        return max(float(minutes * 60) + DEFAULT_ACTION_LISTEN_BUFFER_SECONDS, capture_seconds)
    return capture_seconds


def capture_probes(sections: tuple[str, ...]) -> list[tuple[str, bytes]]:
    probes: list[tuple[str, bytes]] = []
    if "status" in sections:
        probes.append(("status", protocol.pack_commit()))
    if "firmware" in sections:
        probes.append(("firmware", protocol.pack_get_firmware_version()))
    if "names" in sections:
        probes.append(("output_names", protocol.pack_get_station_names()))
    if "schedule" in sections:
        probes.append(("irrigation_config", protocol.pack_get_irrigation_config()))
    return probes


def describe_notification(
    payload: bytes,
    *,
    probe: str = "",
    irrigation_first_fragments: dict[int, int] | None = None,
) -> str:
    if len(payload) >= 17 and payload[0] == 0x0F and payload[2] == 0x01:
        return f"firmware={payload[12]}.{payload[13]}.{payload[14]}"
    if len(payload) >= 17 and payload[0] == 0x10 and payload[1] == 0x0F and payload[2] == 0x01:
        return f"firmware={payload[12]}.{payload[13]}.{payload[14]}"
    if len(payload) >= 20 and payload[0] in (0x35, 0x36) and payload[1] == 0x12:
        station = payload[3] + 1
        fragment = payload[4:20].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        return (
            f"output_name station={station} fragment={payload[2] & 1} "
            f"text={fragment!r}"
        )
    normalized = protocol.normalize_config_notification(payload)
    if normalized is not None:
        program_class = normalized[3] >> 4
        program_index = normalized[3] & 0x0F
        fragment_id = normalized[2]
        if program_class == 1 and irrigation_first_fragments is not None:
            first_fragment_id = max(
                irrigation_first_fragments.get(program_index, fragment_id),
                fragment_id,
            )
            irrigation_first_fragments[program_index] = first_fragment_id
            logical_chunk = first_fragment_id - fragment_id
            chunk_label = IRRIGATION_CHUNK_NAMES.get(
                logical_chunk, f"chunk_{logical_chunk}"
            )
            return (
                f"irrigation program={program_index} fragment={fragment_id} "
                f"{chunk_label}"
            )
        return (
            f"config fragment={fragment_id} class={program_class} "
            f"program_index={program_index}"
        )
    status = protocol.parse_status_notification(payload)
    if status is not None:
        parts = [
            f"status controller={status['controller_state']}",
            f"watering={status['is_watering']}",
            f"station={status.get('station_num')}",
        ]
        if status.get("active_program") is not None:
            parts.append(f"program={status['active_program']}")
        if status.get("watering_origin") is not None:
            parts.append(f"origin={status['watering_origin']}")
        if status.get("remaining_seconds") is not None:
            parts.append(f"remaining={status['remaining_seconds']}s")
        return " ".join(parts)
    if len(payload) >= 3 and payload[2] in (0x00, 0x01, 0x02):
        return f"notify seq=0x{payload[2]:02x}"
    if probe:
        return probe
    return ""


def load_capture_events(
    paths: list[Path],
    *,
    probe: str | None = None,
    direction: str = "RX",
) -> tuple[list[dict[str, Any]], list[Path]]:
    events: list[dict[str, Any]] = []
    used_paths: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        file_events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("direction") != direction:
                continue
            if probe is not None and event.get("probe") != probe:
                continue
            file_events.append(event)
        if file_events:
            events.extend(file_events)
            used_paths.append(path)
    return events, used_paths


def write_capture_event(output: TextIO, event: CaptureEvent, *, verbose: bool) -> None:
    output.write(json.dumps(asdict(event), sort_keys=True) + "\n")
    output.flush()
    if verbose or event.direction == "TX":
        suffix = f" | {event.note}" if event.note else ""
        print(
            f"{event.elapsed_seconds:8.3f}s {event.direction} "
            f"{event.probe:16s} {event.payload_hex}{suffix}"
        )


def format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "disabled"
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def format_status(status: Mapping[str, Any]) -> str:
    parts = [
        f"controller={status.get('controller_state')}",
        f"watering={status.get('is_watering')}",
    ]
    if status.get("station_num") is not None:
        parts.append(f"station={status.get('station_num')}")
    if status.get("active_program") is not None:
        parts.append(f"program={status['active_program']}")
    if status.get("watering_origin") is not None:
        parts.append(f"origin={status['watering_origin']}")
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
