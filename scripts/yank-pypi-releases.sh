#!/usr/bin/env bash
# Yank PyPI releases that still contain scrubbed protocol references.
# Usage: PYPI_TOKEN=pypi-... ./scripts/yank-pypi-releases.sh
set -euo pipefail

if [ -z "${PYPI_TOKEN:-}" ]; then
  echo "Set PYPI_TOKEN to a PyPI API token with yank permission." >&2
  exit 1
fi

reason="Release contained internal protocol references removed in v0.1.14+."
for ver in 0.1.12 0.1.13; do
  echo "Yanking solem-blip-ble ${ver}..."
  curl -fsS -X POST \
    "https://pypi.org/manage/project/solem-blip-ble/release/${ver}/yank/" \
    -u "__token__:${PYPI_TOKEN}" \
    -F "reason=${reason}"
  echo
done

echo "Done. Verify at https://pypi.org/project/solem-blip-ble/#history"
