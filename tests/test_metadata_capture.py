"""Protocol tests derived from live BL-IP V5 metadata capture."""

from __future__ import annotations

import json
from pathlib import Path

from solem_blip_ble import protocol

FIXTURE = Path(__file__).parent / "fixtures" / "solem_metadata_c8b961d44dcc8.jsonl"

CAPTURE_STATION_NAMES = {
    1: "Eleagnus",
    2: "Stazione 2",
    3: "Stazione 3",
    4: "Stazione 4",
    5: "Vasi",
    6: "Stazione 6",
}


def load_capture_events(probe: str, direction: str = "RX") -> list[bytes]:
    payloads: list[bytes] = []
    for line in FIXTURE.read_text().splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event["probe"] == probe and event["direction"] == direction:
            payloads.append(bytes.fromhex(event["payload_hex"]))
    return payloads


def assemble_station_names(
    payloads: list[bytes], *, max_station_num: int
) -> dict[int, str]:
    """Mirror SolemClient.get_station_names() fragment assembly."""
    fragments: dict[int, dict[int, bytes]] = {}
    station_names: dict[int, str] = {}

    for payload in payloads:
        parsed = protocol.parse_station_name_fragment(payload)
        if parsed is None or not 1 <= parsed["station"] <= max_station_num:
            continue
        station = parsed["station"]
        fragments.setdefault(station, {})[parsed["sequence"]] = parsed["name_bytes"]
        if fragments[station].keys() >= {0, 1}:
            station_fragments = fragments[station]
            station_names[station] = (
                station_fragments[1] + station_fragments[0]
            ).decode("utf-8", errors="replace")
            if len(station_names) == max_station_num:
                break

    return station_names


def test_firmware_version_from_capture():
    payloads = load_capture_events("firmware")
    assert len(payloads) >= 1

    result = protocol.parse_firmware_version_response(payloads[0])
    assert result == {
        "major": 5,
        "minor": 1,
        "patch": 7,
        "raw_hex": "5.1.7",
    }


def test_station_name_fragments_from_capture():
    payloads = load_capture_events("output_names")
    assert len(payloads) == 24

    parsed = [protocol.parse_station_name_fragment(payload) for payload in payloads]
    assert all(fragment is not None for fragment in parsed)


def test_station_names_assembled_from_capture():
    payloads = load_capture_events("output_names")
    names = assemble_station_names(payloads, max_station_num=6)
    assert names == CAPTURE_STATION_NAMES


def test_station_names_capture_respects_max_station_num():
    payloads = load_capture_events("output_names")
    names = assemble_station_names(payloads, max_station_num=2)
    assert names == {1: "Eleagnus", 2: "Stazione 2"}
