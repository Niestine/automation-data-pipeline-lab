"""Exceptions for the incremental collection lab."""


class LabError(Exception):
    """Base error for a failed lab run."""


class ConfigError(LabError):
    """Raised when a config file or value is unusable."""


class UnsafeURL(LabError):
    """Raised when a request would leave the allowlist."""


class OriginTimeout(LabError):
    """Raised by the fixture when a request times out."""


class UnsupportedEncoding(LabError):
    """Raised when a content-coding cannot be removed before hashing."""


class SimulatedCrash(LabError):
    """Injected process death. Callers persist state before raising this."""
