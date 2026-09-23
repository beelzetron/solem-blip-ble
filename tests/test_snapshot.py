"""Tests for complete byte-preserving program snapshots."""

from datetime import date

import pytest

from solem_blip_ble import protocol
from solem_blip_ble.exceptions import InvalidSnapshot
from solem_blip_ble.snapshot import ProgramSnapshot


def _program_frames(index: int) -> list[bytes]:
    program: protocol.IrrigationProgram = {
        "name": f"Program {index}",
        "inter_station_delay": 0,
        "water_budget": 100,
        "cycle": 0,
        "week_days": 0x7F,
        "period_length": 1,
        "synchro_day": 0,
        "period_start_date": date(2026, 9, 23),
        "start_times": [360 if index == 2 else None] + [None] * 7,
        "station_durations": [1200 if station == 5 and index == 2 else 0 for station in range(12)],
    }
    # The public packer intentionally exposes editable A/B/C only. Build the
    # same seven-frame layout from Program A and replace only the storage key.
    packed = protocol.pack_set_irrigation_program(0, program, max_stations=12)
    key = 0x10 | index
    return [
        frame[:2] + bytes([6 - chunk, key]) + frame[4:]
        for chunk, frame in enumerate(packed)
    ]


def test_complete_snapshot_preserves_all_twelve_slots() -> None:
    frames = tuple(
        frame
        for index in range(12)
        for frame in _program_frames(index)
    )
    snapshot = ProgramSnapshot.from_frames(frames)

    assert set(snapshot.additional_programs()) == set(range(3, 12))
    assert len(snapshot.write_frames()) == 84
    assert {frame[3] & 0x0F for frame in snapshot.write_frames()} == set(range(12))


def test_snapshot_revision_covers_hidden_slots() -> None:
    frames = [
        frame
        for index in range(12)
        for frame in _program_frames(index)
    ]
    before = ProgramSnapshot.from_frames(tuple(frames))
    changed = list(frames)
    hidden = next(i for i, frame in enumerate(changed) if (frame[3] & 0x0F) == 11)
    changed[hidden] = changed[hidden][:-1] + bytes([changed[hidden][-1] ^ 1])
    after = ProgramSnapshot.from_frames(tuple(changed))

    assert before.revision != after.revision


def test_expected_write_is_tied_to_preflight_snapshot() -> None:
    frames = tuple(
        frame for index in range(12) for frame in _program_frames(index)
    )
    current = ProgramSnapshot.from_frames(frames)
    writes, expected = current.patch(
        2, {"station_durations": {6: 900}}, physical_stations=12
    )

    current.validate_expected_write(writes, expected)

    unrelated_frames = list(expected.frames)
    hidden = next(
        i
        for i, frame in enumerate(unrelated_frames)
        if (frame[3] & 0x0F) == 11
    )
    unrelated_frames[hidden] = (
        unrelated_frames[hidden][:-1]
        + bytes([unrelated_frames[hidden][-1] ^ 1])
    )
    unrelated = ProgramSnapshot.from_frames(tuple(unrelated_frames))
    with pytest.raises(
        InvalidSnapshot, match="not covered by write frames"
    ):
        current.validate_expected_write(writes, unrelated)


def test_expected_write_rejects_payload_not_matching_write_frames() -> None:
    frames = tuple(
        frame for index in range(12) for frame in _program_frames(index)
    )
    current = ProgramSnapshot.from_frames(frames)
    writes, expected = current.patch(
        2, {"station_durations": {6: 900}}, physical_stations=12
    )
    tampered = list(writes)
    tampered[0] = tampered[0][:-1] + bytes([tampered[0][-1] ^ 1])

    with pytest.raises(
        InvalidSnapshot, match="does not match requested write frames"
    ):
        current.validate_expected_write(tampered, expected)
