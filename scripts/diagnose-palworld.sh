#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$SCRIPT_HOME/palworld_manager.py"
CONFIG_DIR="${PALWORLD_CONFIG_DIR:-$(dirname -- "$SCRIPT_HOME")/config}"
OUTPUT_ARGS=()

while (( $# > 0 )); do
  case "$1" in
    --config-dir)
      [[ $# -ge 2 ]] || { printf '%s\n' '--config-dir requires a directory' >&2; exit 2; }
      CONFIG_DIR="$2"; shift 2 ;;
    --json) OUTPUT_ARGS+=(--json); shift ;;
    *) printf 'Usage: %s [--config-dir DIRECTORY] [--json]\n' "$0" >&2; exit 2 ;;
  esac
done

[[ -r "$MANAGER" ]] || { printf 'Configuration manager is missing: %s\n' "$MANAGER" >&2; exit 2; }
exec python3 "$MANAGER" --config-dir "$CONFIG_DIR" --diagnose "${OUTPUT_ARGS[@]}"
