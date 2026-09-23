# solem-blip-ble

Python library for the Solem BL-IP Bluetooth irrigation controller.

## Protocol sources

| Layer | Source |
|-------|--------|
| **Commands** (turn on/off, sprinkle, stop, commit) | [pcman75/solem-blip-reverse-engineering](https://github.com/pcman75/solem-blip-reverse-engineering) — GATT write `108b0002-...`, frame `3105 …` + `3b00` |
| **Status polling** (notify, seq `0x02`, active station, remaining time, battery) | Live BL-IP V5 hardware validation and capture-backed regression tests; see [docs/ble_protocol.md](docs/ble_protocol.md) |

Turn-off-for-N-days is capped at **15 days** per the pcman75 documentation.

## Install

```bash
pip install solem-blip-ble
# or from GitHub:
pip install "solem-blip-ble @ git+https://github.com/beelzetron/solem-blip-ble.git@main"
# or editable for development (prefer uv; deps are hash-locked via uv.lock):
uv sync --frozen
```

## CI/CD

- **CI:** GitHub Actions runs tests (Python 3.11–3.13) and verifies the package builds on every push/PR to `main`.
- **CD:** Creating a [GitHub Release](https://github.com/beelzetron/solem-blip-ble/releases) publishes the package to [PyPI](https://pypi.org/project/solem-blip-ble/) via trusted publishing.

Configure PyPI trusted publishing for this repository: PyPI project → Publishing → Add GitHub Actions publisher (`beelzetron/solem-blip-ble`, workflow `publish.yml`, environment `pypi`).

## Usage

```python
from solem_blip_ble import SolemClient, SolemConnectionError

client = SolemClient("AA:BB:CC:DD:EE:FF", bluetooth_timeout=30)
await client.connect()
status = await client.get_status()
await client.sprinkle_station_x_for_y_minutes(1, 5)
await client.stop_manual_sprinkle()
await client.disconnect()
```

## Documentation

Full BLE protocol notes: [docs/ble_protocol.md](docs/ble_protocol.md)

## Validation CLI

The packaged debug tool is the single supported raw capture path:

```bash
validate-solem-blip AA:BB:CC:DD:EE:FF --capture --verbose
validate-solem-blip AA:BB:CC:DD:EE:FF --capture --only status
validate-solem-blip AA:BB:CC:DD:EE:FF --capture-off-days 3 --verbose
validate-solem-blip AA:BB:CC:DD:EE:FF --replay btsnoop/captures/capture.jsonl
```

Regression tests also replay public capture-backed fixtures for V5 firmware,
station names, status, and persisted irrigation schedule parsing.

## Home Assistant

Used by the [Solem BL-IP for Home Assistant](https://github.com/beelzetron/solem-blip-ha) integration ([HACS](https://github.com/beelzetron/solem-blip-ha#installation)).

## Credits

Thanks to [pcman75](https://github.com/pcman75) for the original Solem BL-IP command reverse engineering.


## Firmware 5 program snapshots and transactional restore

Firmware-5 BL-IP program configuration is represented as a complete raw snapshot
of **12 program slots / 84 frames**. The public snapshot helpers preserve the raw
frames and expose a revision fingerprint so callers can detect stale state before
a mutation. Home Assistant currently edits/restores A/B/C while preserving the
nine additional slots byte-for-byte.

`write_program_frames()` is deliberately conservative:

1. It performs a retry-safe fresh snapshot preflight and checks the caller's
   expected pre-write revision.
2. It then opens the non-retryable mutation transaction, enables notifications,
   writes the requested program blocks and waits for their acknowledgements.
3. After mutation starts, transport failures are reported as `UncertainWrite`;
   the library does **not** automatically replay writes.
4. A complete program readback is performed on the mutation connection and must
   match the caller-provided expected snapshot before the operation is confirmed.

The existing single-program write path (`write_irrigation_program()`, used by
`set_irrigation_program()`) now sends each of the seven V5 frames in its own
fresh BLE session, without a notification subscription. This deliberately keeps
that low-level path minimal for Bluetooth-proxy links, but it changes its
transport profile from the previous one-session frame sequence and can therefore
be slower on a single-connection controller.

An explicit controller rejection is reported as `ProgramWriteRejected`, a
subclass of `UncertainWrite`. Callers can distinguish the rejection from an
unknown transport outcome and refresh/reconcile accordingly; the subtype remains
conservative because earlier blocks in a multi-block transaction may already
have been acknowledged.

The revision preflight currently occurs on a **separate BLE connection** before
the mutation transaction. It is therefore a stale-state guard, not an atomic
same-connection compare-and-swap; callers should keep their own durable journal
when they need crash/transport recovery semantics.

The original BL-IP firmware may normalize controller-owned date metadata while
programs are written. Applications that build an expected snapshot should avoid
replaying stale controller-owned date values and should reconcile only explicitly
understood normalization differences.

These write APIs target the original BL-IP running firmware **5.x**. BL-IP V2 /
firmware 6.x is not supported by this protocol implementation.
