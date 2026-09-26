"""Tests for byte-preserving V5 station-name snapshots.

Mirrors the fork's tests (ThomasHFWright/solem-blip-ha PR #1,
tests/test_station_names.py, protocol-level cases) adapted to the
solem_blip_ble library architecture.
"""

from __future__ import annotations

import pytest

from solem_blip_ble import protocol
from solem_blip_ble.exceptions import InvalidSnapshot
from solem_blip_ble.station_names import StationNameSnapshot


def name_frames(snapshot: StationNameSnapshot) -> list[bytes]:
    """Build a plausible hardware-style fragment stream for a snapshot."""
    result = []
    for station, raw in sorted(snapshot.raw_names.items()):
        seq = (len(snapshot.raw_names) - station) * 2
        result += [
            bytes([0x36, 0x12, seq + 1, station - 1]) + raw[:16],
            bytes([0x36, 0x12, seq, station - 1]) + raw[16:],
        ]
    return result


@pytest.fixture
def snapshot() -> StationNameSnapshot:
    return StationNameSnapshot(
        {i: f"Station {i}".encode().ljust(32, b"\0") for i in range(1, 13)}
    )


def test_complete_names_preserve_unused_slots_unicode_and_order(snapshot):
    before = snapshot
    after = before.renamed(6, "a" * 15 + "é" + "b" * 15, 6)
    frames = name_frames(after)
    actual = StationNameSnapshot.from_frames(list(reversed(frames)) + [frames[0]], 6)
    assert actual.revision == after.revision
    assert actual.names[6] == "a" * 15 + "é" + "b" * 15
    assert all(
        actual.raw_names[i] == before.raw_names[i] for i in range(1, 13) if i != 6
    )


@pytest.mark.parametrize("missing", [0, 5, 22, 23])
def test_missing_fragment_rejects_snapshot(snapshot, missing):
    frames = name_frames(snapshot)
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames(
            frames[:missing] + frames[missing + 1 :], 6
        )


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"\x36\x12\x00\x0c" + bytes(16),
        b"\x00" * 20,
    ],
)
def test_malformed_fragment_rejects_snapshot(snapshot, bad):
    frames = name_frames(snapshot)
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames(frames + [bad], 6)


def test_short_fragment_rejects_snapshot(snapshot):
    frames = name_frames(snapshot)
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames(frames + [frames[0][:-1] + b"X"], 6)


def test_conflicting_fragment_rejects_snapshot(snapshot):
    frames = name_frames(snapshot)
    conflict = bytes([0x36, 0x12, frames[0][2], frames[0][3]]) + b"X" * 16
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames(frames + [conflict], 6)


def test_empty_and_partial_responses_reject_snapshot(snapshot):
    frames = name_frames(snapshot)
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames([], 6)
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames(frames[12:], 6)


def test_non_contiguous_sequences_reject_snapshot(snapshot):
    frames = [bytearray(f) for f in name_frames(snapshot)]
    frames[0][2] = frames[0][2] + 8  # gap in the countdown sequence
    with pytest.raises(InvalidSnapshot):
        StationNameSnapshot.from_frames([bytes(f) for f in frames], 6)


def test_renamed_only_changes_selected_output(snapshot):
    after = snapshot.renamed(3, "Front lawn east", 6)
    assert after.names[3] == "Front lawn east"
    assert all(
        after.raw_names[i] == snapshot.raw_names[i] for i in range(1, 13) if i != 3
    )
    frames = protocol.pack_station_name(3, "Front lawn east", 6)
    assert after.raw_names[3] == frames[0][4:] + frames[1][4:]


def test_revision_is_order_independent_and_content_sensitive(snapshot):
    a = snapshot.renamed(1, "Alpha", 6)
    b = snapshot.renamed(2, "Beta", 6)
    assert a.revision != b.revision
    rebuilt = StationNameSnapshot(dict(a.raw_names))
    assert rebuilt.revision == a.revision


def test_from_fragments_matches_from_frames(snapshot):
    frames = name_frames(snapshot.renamed(6, "Garden", 6))
    fragments = [
        {
            "station": f[3] + 1,
            "sequence": f[2] & 1,
            "name_bytes": f[4:20].split(b"\0", 1)[0],
        }
        for f in frames
    ]
    from_parsed = StationNameSnapshot.from_fragments(fragments, 6)
    from_raw = StationNameSnapshot.from_frames(frames, 6)
    assert from_parsed.names == from_raw.names
    assert from_parsed.revision == from_raw.revision
