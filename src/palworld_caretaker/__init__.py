"""Portable, fail-closed operations core for Palworld Caretaker.

The package deliberately has no Linux or third-party dependency.  Systemd,
filesystem mount checks, and command execution are injected adapters.
"""

from .backup import BackupEngine, BackupManager, BackupResult, RestoreResult, SnapshotError
from .audit import AuditLog, sanitize
from .config import CaretakerConfig, ConfigError, ConfigSchema, load_config, load_env
from .diagnostics import ServerDiagnostic, ServerDiagnostics, SystemMetrics, collect_system_metrics
from .rest import ActionResult, ApiError, Metrics, PalworldRESTClient, Player, RESTClient
from .service import (
    RestCommandChannel, ServerLifecycle,
    ServerStatus, ServiceState, SystemdServiceController,
)
from .settings import CaretakerOptions, SettingSpec, WorldSettings
from .steamcmd import SteamCMD, SteamCMDError
from .web import WebDependencies, WebServer, create_server

__version__ = "0.4.1"

__all__ = [
    "__version__", "ActionResult", "ApiError", "AuditLog", "BackupEngine", "BackupManager", "BackupResult", "CaretakerConfig", "CaretakerOptions", "ConfigError",
    "ConfigSchema", "Metrics", "PalworldRESTClient", "Player", "RESTClient", "RestCommandChannel", "RestoreResult", "ServerDiagnostic",
    "ServerDiagnostics", "ServerLifecycle", "ServerStatus", "ServiceState", "SettingSpec", "SnapshotError", "SteamCMD", "SteamCMDError", "SystemMetrics", "SystemdServiceController", "WorldSettings",
    "WebDependencies", "WebServer", "collect_system_metrics", "create_server", "load_config", "load_env", "sanitize",
]
