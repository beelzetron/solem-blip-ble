# solem-blip-ble Agent Guidelines

## Project Overview

Python BLE client library for Solem BL-IP Bluetooth irrigation controllers. Uses `bleak` for BLE communication.

## Structure

- **Source:** `src/solem_blip_ble/` (main package)
- **Tests:** `tests/` (pytest with asyncio auto mode)
- **Protocol docs:** `docs/ble_protocol.md`
- **Scripts:** `scripts/` (validation utilities)

## Developer Commands

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -v

# Build distributions
python -m build

# Test on specific Python version
python -m pytest -v tests/test_client.py
```

## CI/CD Flow

- **CI:** GitHub Actions tests on Python 3.11–3.14, builds sdist/wheel
- **CD:** Creating a GitHub Release triggers PyPI publish via trusted publishing

## Branching and releases

Use the lightweight Git Flow policy in `docs/branching_and_release.md`.

- Do not commit directly to `main` for normal work.
- Start changes from `feature/<topic>`, `fix/<topic>`, or `hotfix/<topic>`.
- Merge to `main` through a pull request after CI passes.
- Cut GitHub releases only from merged `main`.
- Use immutable `-beta.N` or `-rc.N` pre-releases for changes that need live HA or hardware validation before stable release.
- Commit version-only release bumps directly to `main` after candidate PR CI is green; do not open release-bump PRs.
- BLE CI skips test/mypy jobs for version-only release-bump pushes and runs only distribution build sanity.
- Keep release commits scoped to the BLE library; do not combine HA integration changes.

## Protocol Implementation Notes

**Critical BLE details** (see `docs/ble_protocol.md` for full spec):

1. **All commands require a commit:** Every command frame (`3105...`) must be followed by `3b00` to execute
2. **Only parse seq `0x02` notifications:** Sequence `0x01` is intermediate, `0x00` is final/empty
3. **Remaining time offset:** Bytes 13-14 (big-endian uint16), NOT 14-16 (padding)
4. **Battery voltage:** Byte 10 (raw 9V reading, map to icon level 0-5)
5. **Status byte (byte 3):**
   - `0x40`: Controller ON, idle
   - `0x42`: Controller ON, actively watering
   - `0x02`: Controller OFF, manual watering active
   - `0x00`: Controller OFF, idle

## Testing Guidelines

- Tests use `pytest-asyncio` in `auto` mode
- Fixtures in `tests/fixtures/`
- No external BLE device required for unit tests (mocked)
- Integration tests require actual BL-IP hardware

## Key Files

- `src/solem_blip_ble/client.py` - Main `SolemClient` class
- `src/solem_blip_ble/protocol.py` - Protocol frame encoding/decoding
- `src/solem_blip_ble/const.py` - Constants and command definitions
- `scripts/validate_device.py` - Real-device validation script

## Version Support

- Python: 3.11+
- BLE library: bleak >= 0.22.3
