"""Complete program snapshots and byte-preserving V5 program edits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from typing import Any

from . import protocol
from .exceptions import SolemConnectionError


class InvalidSnapshot(SolemConnectionError):
    """The configuration cannot be safely interpreted or edited."""


class StaleProgram(SolemConnectionError):
    """The controller changed since the draft was opened."""


class UncertainWrite(SolemConnectionError):
    """A mutation may have reached the controller; never replay it."""


@dataclass(frozen=True)
class ProgramSnapshot:
    """Keep all raw bytes, including fields not displayed in Home Assistant."""

    frames: tuple[bytes, ...]
    blocks: dict[int, tuple[bytes, ...]]
    extras: tuple[bytes, ...]
    programs: dict[int, protocol.IrrigationProgram]

    @classmethod
    def from_frames(cls, frames: tuple[bytes, ...]) -> ProgramSnapshot:
        if not protocol.irrigation_config_complete(frames):
            raise InvalidSnapshot("Incomplete A/B/C program response")
        groups: dict[int, dict[int, bytes]] = {i: {} for i in range(3)}
        extras: set[bytes] = set()
        for raw in frames:
            frame = protocol.normalize_config_notification(raw)
            if frame is None:
                continue
            if frame[3] >> 4 != 1:
                extras.add(frame)
                continue
            index = frame[3] & 15
            if index not in groups:
                extras.add(frame)
                continue
            old = groups[index].get(frame[2])
            if old is not None and old != frame:
                raise InvalidSnapshot("Conflicting configuration fragments")
            groups[index][frame[2]] = frame
        blocks: dict[int, tuple[bytes, ...]] = {}
        for index, group in groups.items():
            if len(group) != 7 or max(group) - min(group) != 6:
                raise InvalidSnapshot("Unknown program fragment layout")
            # Canonical sequence IDs avoid incidental global fragment numbering.
            blocks[index] = tuple(
                frame[:2] + bytes([chunk]) + frame[3:]
                for chunk, (_, frame) in enumerate(sorted(group.items(), reverse=True))
            )
        return cls(frames, blocks, tuple(sorted(extras)),
                   protocol.assemble_irrigation_programs(frames, max_stations=12))

    @property
    def revision(self) -> str:
        """Fingerprint every preserved program and additional config frame."""
        digest = hashlib.sha256()
        for frame in (*sum((self.blocks[i] for i in range(3)), ()), *self.extras):
            digest.update(len(frame).to_bytes(2, "big"))
            digest.update(frame)
        return digest.hexdigest()

    def require_known_programs(self) -> None:
        """Validate additional storage slots without making them editable."""
        self.additional_programs()

    def additional_programs(self) -> dict[int, protocol.IrrigationProgram]:
        """Decode the nine additional slots in the twelve-slot V5 response."""
        groups: dict[int, list[bytes]] = {}
        for frame in self.extras:
            if frame[3] >> 4 == 1:
                groups.setdefault(frame[3] & 15, []).append(frame)
        if groups and set(groups) != set(range(3, 12)):
            raise InvalidSnapshot("Unknown program slot layout; configuration preserved for inspection")
        programs = {}
        for index, group in groups.items():
            ordered = sorted(group, key=lambda frame: frame[2], reverse=True)
            if (len(ordered) != 7 or ordered[0][2] - ordered[-1][2] != 6
                or len({frame[2] for frame in ordered}) != 7
                or tuple(map(len, ordered)) != (20, 20, 16, 20, 19, 19, 10)):
                raise InvalidSnapshot("Unknown additional program fragment layout")
            remapped = [frame[:3] + b"\x10" + frame[4:] for frame in ordered]
            programs[index] = protocol.assemble_irrigation_programs(remapped, max_stations=12)[0]
        return programs

    def patch(
        self, index: int, changes: dict[str, Any], physical_stations: int
    ) -> tuple[list[bytes], ProgramSnapshot]:
        """Patch fields into existing bytes; never invent unused values/dates."""
        self.require_known_programs()
        if index not in self.blocks or not 1 <= physical_stations <= 12:
            raise ValueError("Invalid program or station count")
        supported = {"name", "inter_station_delay", "water_budget", "cycle", "week_days",
                     "period_length", "synchro_day", "period_start_date", "start_times",
                     "station_durations"}
        if set(changes) - supported:
            raise ValueError("Unsupported program field")
        blocks = [bytearray(frame) for frame in self.blocks[index]]
        if self.programs[index]["cycle"] not in range(5):
            raise InvalidSnapshot("Unsupported cycle mode")
        # Settings without a complete date must remain byte-for-byte intact.
        expected_lengths = (20, 20, 16, 20, 19, 19, 10)
        if any(len(frame) != length for frame, length in zip(blocks, expected_lengths)):
            raise InvalidSnapshot("Unsupported program block lengths")

        def integer(value: Any, maximum: int, minimum: int = 0) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"Expected integer in {minimum}..{maximum}")
            return value

        for key, value in changes.items():
            if key == "name":
                if not isinstance(value, str) or "\x00" in value or len(value.encode()) > 31:
                    raise ValueError("Program name must fit 31 UTF-8 bytes without NUL")
                encoded = value.encode().ljust(31, b"\0")
                blocks[0][4:20] = encoded[:16]
                blocks[1][4:19] = encoded[16:]
            elif key in ("water_budget", "inter_station_delay"):
                offset = 6 if key == "water_budget" else 4
                blocks[2][offset:offset + 2] = integer(value, 65535).to_bytes(2, "big")
            elif key in ("cycle", "week_days", "period_length", "synchro_day"):
                offset, maximum, minimum = {
                    "cycle": (8, 4, 0), "week_days": (9, 127, 0),
                    "period_length": (10, 255, 1), "synchro_day": (11, 255, 0),
                }[key]
                blocks[2][offset] = integer(value, maximum, minimum)
            elif key == "period_start_date":
                if isinstance(value, str):
                    value = date.fromisoformat(value)
                if not isinstance(value, date):
                    raise ValueError("A valid interval start date is required")
                blocks[2][12:16] = bytes([value.day, value.month]) + value.year.to_bytes(2, "big")
            elif key == "start_times":
                if not isinstance(value, list) or len(value) != 8:
                    raise ValueError("Exactly eight start slots are required")
                for slot, minutes in enumerate(value):
                    minutes = 1440 if minutes is None else integer(minutes, 1439)
                    blocks[3][4 + slot * 2:6 + slot * 2] = minutes.to_bytes(2, "big")
            elif key == "station_durations":
                if not isinstance(value, dict):
                    raise ValueError("Station durations must be a station/seconds patch")
                for station, seconds in value.items():
                    station = integer(station, physical_stations, 1) - 1
                    chunk, slot = (4, station) if station < 5 else ((5, station - 5) if station < 10 else (6, station - 10))
                    blocks[chunk][4 + slot * 3:7 + slot * 3] = integer(seconds, 0xFFFFFF).to_bytes(3, "big")

        if blocks[2][8] == 4:
            parsed_date = protocol.parse_period_start_date(blocks[2])
            if parsed_date is None or not blocks[2][10] or blocks[2][11] >= blocks[2][10]:
                raise InvalidSnapshot("Interval date/phase cannot be safely preserved")
        changed = [i for i, block in enumerate(blocks) if bytes(block) != self.blocks[index][i]]
        if not changed:
            return [], self
        # Send the complete seven-block sequence, preserving unchanged bytes.
        # No manual 3b00 commit. Preserve every byte outside the field patch.
        writes = []
        for chunk, frame in enumerate(blocks):
            writes.append(bytes([0x2F if chunk < 2 else 0x37, len(frame) - 2,
                                chunk if chunk < 2 else chunk - 2, frame[3]]) + bytes(frame[4:]))
        # Reconstruct a response to calculate the exact expected snapshot.
        expected_frames: list[bytes] = []
        normalized = [protocol.normalize_config_notification(raw) for raw in self.frames]
        first = max(frame[2] for frame in normalized if frame is not None and frame[3] == 0x10 + index)
        for raw, response_frame in zip(self.frames, normalized):
            if response_frame is not None and response_frame[3] == 0x10 + index:
                expected_frames.append(response_frame[:4] + bytes(blocks[first - response_frame[2]][4:]))
            else:
                expected_frames.append(raw)
        return writes, self.from_frames(tuple(expected_frames))
