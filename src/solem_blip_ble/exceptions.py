"""Exceptions for Solem BL-IP BLE client."""


class SolemConnectionError(Exception):
    """Raised when a BLE connection or device operation fails."""


class SolemDeadlineExceeded(Exception):
    """Raised when an operation exceeds its whole-operation deadline.

    Deliberately NOT a subclass of SolemConnectionError: retry paths only
    retry SolemConnectionError, so a deadline breach surfaces to the caller
    instead of re-entering the retry loop that just consumed its budget.
    """
