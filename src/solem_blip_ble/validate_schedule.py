"""Backward-compatible entry point focused on irrigation schedule reads."""

from __future__ import annotations

from solem_blip_ble.validate import main as validate_main


def main() -> int:
    return validate_main(default_only=["schedule"])


if __name__ == "__main__":
    raise SystemExit(main())
