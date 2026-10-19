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
        "period_start_date": None,
        "controller_date": None,
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


def _interval_frames(
    *, period_length: int = 3, synchro_day: int = 1, today: date = date(2026, 6, 1)
) -> tuple[bytes, ...]:
    """Build the seven frames of each of the three cycle-4 interval programs."""
    frames: list[bytes] = []
    for index in range(3):
        program: protocol.IrrigationProgram = {
            "name": "Siepe",
            "inter_station_delay": 0,
            "water_budget": 100,
            "cycle": 4,
            "week_days": 0x7F,
            "period_length": period_length,
            "synchro_day": synchro_day,
            "period_start_date": None,
            "controller_date": None,
            "start_times": [270, None, None, None, None, None, None, None],
            "station_durations": [0, 1800] + [0] * 10,
        }
        packed = protocol.pack_set_irrigation_program(
            0, program, today=today, max_stations=12
        )
        frames.extend(
            # Canonical descending fragment ids, as a real A/B/C read produces.
            frame[:2] + bytes([6 - chunk, 0x10 | index]) + frame[4:]
            for chunk, frame in enumerate(packed)
        )
    return tuple(frames)


def _mutate_header(frames: tuple[bytes, ...], offset: int, value: int) -> tuple[bytes, ...]:
    """Return ``frames`` with one byte of program A's header chunk replaced."""
    mutated = list(frames)
    index = next(
        i
        for i, frame in enumerate(mutated)
        if frame[1] == 0x0E and frame[3] == 0x10
    )
    header = bytearray(mutated[index])
    header[offset] = value
    mutated[index] = bytes(header)
    return tuple(mutated)


def test_patch_rejects_period_start_date() -> None:
    """The interval anchor is a write input, not a patchable stored field."""
    snapshot = ProgramSnapshot.from_frames(_interval_frames())

    with pytest.raises(
        ValueError, match=r"Unsupported program field\(s\): \['period_start_date'\]"
    ):
        snapshot.patch(
            0, {"period_start_date": date(2026, 9, 23)}, physical_stations=12
        )


def test_patch_rejects_cycle_4_synchro_not_below_period() -> None:
    snapshot = ProgramSnapshot.from_frames(
        _mutate_header(_interval_frames(period_length=3, synchro_day=1), 11, 3)
    )

    with pytest.raises(InvalidSnapshot, match="synchro_day"):
        snapshot.patch(0, {"water_budget": 50}, physical_stations=12)


def test_patch_rejects_cycle_4_zero_period_length() -> None:
    snapshot = ProgramSnapshot.from_frames(
        _mutate_header(_interval_frames(period_length=3, synchro_day=1), 10, 0)
    )

    with pytest.raises(InvalidSnapshot, match="period_length 0 and synchro_day 1"):
        snapshot.patch(0, {"water_budget": 50}, physical_stations=12)


def test_patch_rejects_patched_synchro_at_period_length() -> None:
    """The guard re-checks the patched slot, not just the captured bytes."""
    snapshot = ProgramSnapshot.from_frames(
        _interval_frames(period_length=3, synchro_day=1)
    )

    with pytest.raises(InvalidSnapshot, match="period_length 3 and synchro_day 3"):
        snapshot.patch(0, {"synchro_day": 3}, physical_stations=12)


def test_patch_rejects_patched_period_length_below_synchro() -> None:
    """Shrinking period_length under the stored countdown is rejected."""
    snapshot = ProgramSnapshot.from_frames(
        _interval_frames(period_length=3, synchro_day=2)
    )

    with pytest.raises(InvalidSnapshot, match="period_length 1 and synchro_day 2"):
        snapshot.patch(0, {"period_length": 1}, physical_stations=12)


def test_patch_cycle_4_preserves_controller_date_bytes() -> None:
    snapshot = ProgramSnapshot.from_frames(_interval_frames())

    writes, expected = snapshot.patch(0, {"synchro_day": 2}, physical_stations=12)
    header = next(frame for frame in writes if frame[1] == 0x0E)

    assert header[11] == 2
    assert header[12:16] == bytes([1, 6]) + (2026).to_bytes(2, "big")
    snapshot.validate_expected_write(writes, expected)


def _twelve_slot_interval_frames() -> tuple[bytes, ...]:
    """Build a complete twelve-slot capture whose headers are cycle-4 intervals."""
    frames: list[bytes] = []
    for index in range(12):
        program: protocol.IrrigationProgram = {
            "name": "Siepe",
            "inter_station_delay": 0,
            "water_budget": 100,
            "cycle": 4,
            "week_days": 0x7F,
            "period_length": 3,
            "synchro_day": 1,
            "period_start_date": None,
            "controller_date": None,
            "start_times": [270, None, None, None, None, None, None, None],
            "station_durations": [0, 1800] + [0] * 10,
        }
        packed = protocol.pack_set_irrigation_program(
            0, program, today=date(2026, 6, 1), max_stations=12
        )
        frames.extend(
            frame[:2] + bytes([6 - chunk, 0x10 | index]) + frame[4:]
            for chunk, frame in enumerate(packed)
        )
    return tuple(frames)


def test_write_frames_echoes_captured_cycle_4_header_without_revalidating() -> None:
    """A restore reproduces captured bytes, even outside the interval invariant."""
    captured = _mutate_header(_twelve_slot_interval_frames(), 11, 3)
    snapshot = ProgramSnapshot.from_frames(captured)

    writes = snapshot.write_frames()
    written_header = next(
        frame for frame in writes if frame[1] == 0x0E and frame[3] == 0x10
    )
    captured_header = next(
        frame for frame in captured if frame[1] == 0x0E and frame[3] == 0x10
    )

    # synchro_day 3 == period_length 3 violates the client-side invariant, but
    # a restore must echo the captured device state unchanged.
    assert written_header[11] == 3
    assert written_header[4:] == captured_header[4:]


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
