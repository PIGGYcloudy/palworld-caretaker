"""Errors intentionally split by domain for callers and platform adapters."""


class CaretakerError(RuntimeError):
    """Base error raised by the portable caretaker core."""


class ConfigError(CaretakerError):
    """A configuration document or value is invalid."""


class ApiError(CaretakerError):
    """The Palworld REST endpoint cannot be used safely."""


class SnapshotError(CaretakerError):
    """A backup snapshot failed validation or a file operation failed."""


class SteamCMDError(CaretakerError):
    """SteamCMD did not complete an installation/update command."""
