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


def _frame_hex(frame: bytes, limit: int = 8) -> str:
    """First ``limit`` bytes of a frame, for compact error diagnostics."""
    return frame[:limit].hex() + ("…" if len(frame) > limit else "")


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
                raise InvalidSnapshot(
                    f"Invalid station-name response: frame {_frame_hex(frame)}"
                )
            station, part = frame[3] + 1, frame[2] & 1
            group = parts.setdefault(station, {})
            if part in group and group[part] != frame[4:]:
                raise InvalidSnapshot(
                    f"Conflicting station-name fragments: "
                    f"station {station} half {part} "
                    f"({group[part].hex()} vs {frame[4:].hex()})"
                )
            group[part] = frame[4:]
            sequences.add(frame[2])
        details = []
        missing_halves = {
            station: sorted({0, 1} - set(group))
            for station, group in parts.items()
            if set(group) != {0, 1}
        }
        missing_stations = sorted(
            set(range(1, physical_stations + 1)) - parts.keys()
        )
        if missing_stations:
            missing_halves.update({station: [0, 1] for station in missing_stations})
        if missing_halves:
            details.append(
                "missing halves for stations "
                + str(
                    {
                        station: missing
                        for station, missing in sorted(missing_halves.items())
                    }
                )
            )
        # Devices may report more outputs than physical_stations (unused
        # slots); extras are valid, only a shortfall is diagnostic.
        expected_frames = 2 * max(physical_stations, max(parts, default=0))
        if len(frames) < expected_frames:
            details.append(
                f"{len(frames)}/{expected_frames} frames received"
            )
        gaps = sorted(
            set(range(max(sequences, default=-1) + 1)) - sequences
        )
        if gaps:
            details.append(f"sequence gaps: {gaps}")
        if details:
            raise InvalidSnapshot(
                "Incomplete station-name response: " + "; ".join(details)
            )
        return cls({station: group[1] + group[0] for station, group in parts.items()})

    @property
    def names(self) -> dict[int, str]:
        """Decoded UTF-8 names keyed by 1-based output number."""
        return {
            station: raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            for station, raw in self.raw_names.items()
        }

    @property
    def station_count(self) -> int:
        """Highest 1-based output with a non-empty (NUL-stripped) name.

        This is the device-derived physical station count (#57): the
        name-read request asks for *all* output names and the device
        decides what to report, so the highest named output is the
        authoritative width. Whitespace-only names deliberately count as
        named: whatever the device holds in that slot is device-held
        data, and this value must mirror the controller's own notion of
        its width, not an editorial judgement about name quality.
        When the controller also reports unnamed
        (unused) outputs they are ignored here; when no output has a
        non-empty name, the count falls back to the highest reported
        output number. Note: the client adopts this value upward-only
        (it can raise but never lower the client's effective width,
        because the device reports every configured slot even when the
        higher ones carry no onboard names).
        """
        named = [station for station, name in self.names.items() if name]
        return max(named, default=max(self.raw_names, default=0))

    @property
    def reported_count(self) -> int:
        """Number of outputs the device reported in the name response.

        Diagnostic value: it may exceed :attr:`station_count` on devices
        that also report unnamed/unused output slots.
        """
        return len(self.raw_names)

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
