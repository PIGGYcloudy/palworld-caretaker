"""Read-only display of configured storage locations."""
from __future__ import annotations

from .config import CaretakerConfig


def locations_payload(config: CaretakerConfig) -> dict[str, object]:
    """Expose configured paths without accepting filesystem operations."""
    return {
        "locations": [
            {"id": "server", "label": "伺服器檔案", "path": str(config.server_root)},
            {"id": "savegames", "label": "目前世界存檔", "path": str(config.server_root / "Pal/Saved/SaveGames")},
            {"id": "backups", "label": "世界快照", "path": str(config.backup_root)},
        ],
    }
