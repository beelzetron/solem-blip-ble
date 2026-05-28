"""BLE client for Solem BL-IP irrigation controllers."""

from .client import SolemClient
from .exceptions import SolemConnectionError

# Back-compat alias used by Home Assistant integrations
APIConnectionError = SolemConnectionError

__all__ = [
    "SolemClient",
    "SolemConnectionError",
    "APIConnectionError",
]
