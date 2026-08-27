"""Portable, fail-closed operations core for Palworld Caretaker.

The package deliberately has no Linux or third-party dependency.  Systemd,
filesystem mount checks, and command execution are injected adapters.
"""

from .backup import BackupEngine, BackupManager, BackupResult, RestoreResult, SnapshotError
from .config import CaretakerConfig, ConfigError, ConfigSchema, load_config, load_env
from .rest import ActionResult, ApiError, Metrics, PalworldRESTClient, Player, RESTClient
from .service import (
    RestCommandChannel, ServerDiagnostic, ServerDiagnostics, ServerLifecycle,
    ServerStatus, ServiceState, SystemdServiceController,
)
from .steamcmd import SteamCMD, SteamCMDError
from .web import WebDependencies, WebServer, create_server

__all__ = [
    "ActionResult", "ApiError", "BackupEngine", "BackupManager", "BackupResult", "CaretakerConfig", "ConfigError",
    "ConfigSchema", "Metrics", "PalworldRESTClient", "Player", "RESTClient", "RestCommandChannel", "RestoreResult", "ServerDiagnostic",
    "ServerDiagnostics", "ServerLifecycle", "ServerStatus", "ServiceState", "SnapshotError", "SteamCMD", "SteamCMDError", "SystemdServiceController",
    "WebDependencies", "WebServer", "create_server", "load_config", "load_env",
]
