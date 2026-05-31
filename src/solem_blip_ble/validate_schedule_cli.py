"""Console entry point for irrigation schedule read validation."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _schedule_validator_script() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "scripts" / "validate_schedule_read.py"


def main() -> None:
    target = _schedule_validator_script()
    if not target.is_file():
        print(f"Missing validator script: {target}", file=sys.stderr)
        raise SystemExit(1)
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
