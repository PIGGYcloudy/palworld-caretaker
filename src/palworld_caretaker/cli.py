"""Small package entry point; deployment-specific commands remain in scripts/."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import DEFAULTS, ConfigError, load_config
from .paths import native_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palworld Caretaker portable core")
    parser.add_argument(
        "--config-dir", type=native_path,
        default=native_path(DEFAULTS["PALWORLD_INSTALL_ROOT"]) / "config",
    )
    parser.add_argument("--check-config", action="store_true", help="validate configuration without exposing secrets")
    args = parser.parse_args(argv)
    if not args.check_config:
        parser.print_help()
        return 0
    try:
        load_config(args.config_dir)
    except ConfigError as exc:
        parser.error(str(exc))
    print("Palworld configuration is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
