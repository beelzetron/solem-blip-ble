"""V5 output-name snapshots: complete, byte-preserving reads and edits.

The name-read/write frame logic was ported from the hardware-validated
ThomasHFWright fork (solem-blip-ha PR #1, ``ble/station_names.py``) into
this library. A name snapshot is only valid when every reported output
contributed both 16-byte halves and the response sequence IDs are
contiguous from zero — partial or conflicting responses are rejected so
callers never act on an incomplete picture of the controller's names.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from . import protocol
from .exceptions import InvalidSnapshot


@dataclass(frozen=True)
class StationNameSnapshot:
    """Names for every reported output, including unused physical slots."""

    raw_names: dict[int, bytes]

    @classmethod
    def from_frames(
        cls, frames: list[bytes], physical_stations: int
    ) -> StationNameSnapshot:
        """Assemble raw per-output names from 0x35/0x36 name fragments.

        Raises :class:`InvalidSnapshot` when any fragment is malformed,
        any output is missing a half, fragments conflict, or the response
        sequence IDs are not contiguous from zero.
        """
        parts: dict[int, dict[int, bytes]] = {}
        sequences: set[int] = set()
        for frame in frames:
            if (
                len(frame) != 20
                or frame[:2] not in (b"\x36\x12", b"\x35\x12")
                or frame[3] >= 12
            ):
                raise InvalidSnapshot("Invalid station-name response")
            station, part = frame[3] + 1, frame[2] & 1
            group = parts.setdefault(station, {})
            if part in group and group[part] != frame[4:]:
                raise InvalidSnapshot("Conflicting station-name fragments")
            group[part] = frame[4:]
            sequences.add(frame[2])
        if (
            not set(range(1, physical_stations + 1)) <= parts.keys()
            or any(set(group) != {0, 1} for group in parts.values())
            or sequences != set(range(max(sequences, default=-1) + 1))
        ):
            raise InvalidSnapshot("Incomplete station-name response")
        return cls({station: group[1] + group[0] for station, group in parts.items()})

    @property
    def names(self) -> dict[int, str]:
        """Decoded UTF-8 names keyed by 1-based output number."""
        return {
            station: raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            for station, raw in self.raw_names.items()
        }

    @property
    def revision(self) -> str:
        """Stable digest over every raw name; used for stale-edit checks."""
        return hashlib.sha256(
            b"".join(
                bytes([station]) + raw
                for station, raw in sorted(self.raw_names.items())
            )
        ).hexdigest()

    def renamed(
        self, station: int, name: str, physical_stations: int
    ) -> StationNameSnapshot:
        """Return the expected post-write snapshot for one renamed output."""
        frames = protocol.pack_station_name(station, name, physical_stations)
        return StationNameSnapshot(
            {**self.raw_names, station: frames[0][4:] + frames[1][4:]}
        )
