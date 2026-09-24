"""Exceptions for Solem BL-IP BLE client."""


class SolemConnectionError(Exception):
    """Raised when a BLE connection or device operation fails."""


class SolemDeadlineExceeded(Exception):
    """Raised when an operation exceeds its whole-operation deadline.

    Deliberately NOT a subclass of SolemConnectionError: retry paths only
    retry SolemConnectionError, so a deadline breach surfaces to the caller
    instead of re-entering the retry loop that just consumed its budget.
    """


class InvalidSnapshot(SolemConnectionError):
    """The configuration cannot be safely interpreted or edited."""


class StaleProgram(SolemConnectionError):
    """The controller changed since the draft was opened."""


class UncertainWrite(SolemConnectionError):
    """A mutation may have reached the controller; never replay it."""


class ProgramWriteRejected(UncertainWrite):
    """The controller explicitly rejected a program block.

    This is distinct from an unknown transport outcome so callers can choose
    to refresh and reconcile deliberately. It remains an ``UncertainWrite``
    subtype because earlier blocks in the same multi-block transaction may
    already have been acknowledged and applied.
    """


class SolemTimeSyncBusy(SolemConnectionError):
    """A time sync was deferred: the controller is busy (watering or an
    active program) and must not be disturbed."""


class SolemTimeSyncRejected(SolemTimeSyncBusy):
    """The controller explicitly rejected a time update."""


class SolemTimeSyncVerificationFailed(SolemTimeSyncBusy):
    """A time update was acknowledged but the follow-up status read did not
    confirm it, so the sync is not reported as done."""
