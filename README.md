# solem-blip-ble

Python library for the Solem BL-IP Bluetooth irrigation controller.

## Protocol sources

| Layer | Source |
|-------|--------|
| **Commands** (turn on/off, sprinkle, stop, commit) | [pcman75/solem-blip-reverse-engineering](https://github.com/pcman75/solem-blip-reverse-engineering) — GATT write `108b0002-...`, frame `3105 …` + `3b00` |
| **Status polling** (notify, seq `0x02`, station, remaining time) | Live testing on BL-IP hardware; see [docs/ble_protocol.md](docs/ble_protocol.md) |

Turn-off-for-N-days is capped at **15 days** per the pcman75 documentation.

## Install

```bash
pip install solem-blip-ble
# or from GitHub:
pip install "solem-blip-ble @ git+https://github.com/beelzetron/solem-blip-ble.git@main"
# or editable for development:
pip install -e ".[dev]"
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
await client.disconnect()
await client.stop_manual_sprinkle()
```

## Documentation

Full BLE protocol notes: [docs/ble_protocol.md](docs/ble_protocol.md)

## Home Assistant

Used by the [Solem BL-IP for Home Assistant](https://github.com/beelzetron/solem-blip-ha) integration ([HACS](https://github.com/beelzetron/solem-blip-ha#installation)).

## Credits

Thanks to [pcman75](https://github.com/pcman75) for the original Solem BL-IP command reverse engineering.
