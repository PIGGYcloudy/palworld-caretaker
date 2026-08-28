#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

VERSION=0.5.0
OUTPUT_DIR=dist
OUTPUT_FILE=
OUTPUT_DIR_SPECIFIED=0
SOURCE_REF=HEAD

usage() {
  printf 'Usage: %s [--version VERSION] [--output FILE | --output-dir DIRECTORY] [--source-ref GIT_REF]\n' "$0"
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (( $# > 0 )); do
  case "$1" in
    --version) [[ $# -ge 2 ]] || die '--version requires a value'; VERSION=$2; shift 2 ;;
    --output-dir) [[ $# -ge 2 ]] || die '--output-dir requires a directory'; OUTPUT_DIR=$2; OUTPUT_DIR_SPECIFIED=1; shift 2 ;;
    --output) [[ $# -ge 2 ]] || die '--output requires a file'; OUTPUT_FILE=$2; shift 2 ;;
    --source-ref) [[ $# -ge 2 ]] || die '--source-ref requires a Git ref'; SOURCE_REF=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || die 'version is not a valid release identifier'
[[ "$SOURCE_REF" =~ ^[0-9A-Za-z][0-9A-Za-z._/-]*$ ]] || die 'source ref contains unsupported characters'
command -v git >/dev/null || die 'git is required'
command -v gzip >/dev/null || die 'gzip is required'
command -v sha256sum >/dev/null || die 'sha256sum is required'

REPOSITORY="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)" || die 'not inside a Git repository'
git -C "$REPOSITORY" rev-parse --verify "${SOURCE_REF}^{commit}" >/dev/null || die "Git ref does not resolve to a commit: $SOURCE_REF"
git -C "$REPOSITORY" diff --quiet -- || die 'tracked working-tree changes exist; commit or stash them before packaging'
git -C "$REPOSITORY" diff --cached --quiet -- || die 'staged changes exist; commit them before packaging'
git -C "$REPOSITORY" show "$SOURCE_REF:CHANGELOG.md" | grep -Fq "## [$VERSION]" || die "CHANGELOG.md does not contain release $VERSION"

[[ "$OUTPUT_DIR_SPECIFIED" -eq 0 || -z "$OUTPUT_FILE" ]] || die '--output and --output-dir cannot be used together'
if [[ -n "$OUTPUT_FILE" ]]; then
  if [[ "$OUTPUT_FILE" != /* ]]; then OUTPUT_FILE="$REPOSITORY/$OUTPUT_FILE"; fi
  OUTPUT_FILE="$(realpath -m -- "$OUTPUT_FILE")"
  [[ "$OUTPUT_FILE" != / ]] || die 'output file must not be the filesystem root'
  ARCHIVE="$OUTPUT_FILE"
  OUTPUT_DIR="$(dirname -- "$ARCHIVE")"
  ARCHIVE_NAME="$(basename -- "$ARCHIVE")"
else
  if [[ "$OUTPUT_DIR" != /* ]]; then OUTPUT_DIR="$REPOSITORY/$OUTPUT_DIR"; fi
  OUTPUT_DIR="$(realpath -m -- "$OUTPUT_DIR")"
  [[ "$OUTPUT_DIR" != / ]] || die 'output directory must not be the filesystem root'
  ARCHIVE_NAME="palworld-caretaker-v$VERSION.tar.gz"
  ARCHIVE="$OUTPUT_DIR/$ARCHIVE_NAME"
fi
mkdir -p -- "$OUTPUT_DIR"
CHECKSUMS="$OUTPUT_DIR/SHA256SUMS"
TEMPORARY="$(mktemp -d)"
cleanup() { rm -rf -- "$TEMPORARY"; }
trap cleanup EXIT

git -C "$REPOSITORY" archive --format=tar \
  --prefix="palworld-caretaker-v$VERSION/" "$SOURCE_REF" > "$TEMPORARY/release.tar"
gzip -n -9 < "$TEMPORARY/release.tar" > "$TEMPORARY/$ARCHIVE_NAME"

tar -tzf "$TEMPORARY/$ARCHIVE_NAME" > "$TEMPORARY/contents.txt"
grep -qx "palworld-caretaker-v$VERSION/docs/INSTALL.md" "$TEMPORARY/contents.txt" || die 'archive is missing docs/INSTALL.md'
grep -qx "palworld-caretaker-v$VERSION/docs/UPGRADE.md" "$TEMPORARY/contents.txt" || die 'archive is missing docs/UPGRADE.md'
if grep -Eq '(^|/)(\.git|__pycache__|\.pytest_cache|\.DS_Store|server|venv|backups-local|SaveGames|Saved|\.local-backups)(/|$)|(^|/)[^/]+\.env$|(^|/)\.pre-|\.pyc$|\.sav$|\.tar(\.gz)?$' "$TEMPORARY/contents.txt"; then
  die 'archive contains repository metadata, local configuration, or generated files'
fi

mv -f -- "$TEMPORARY/$ARCHIVE_NAME" "$ARCHIVE"
(
  cd -- "$OUTPUT_DIR"
  sha256sum "$ARCHIVE_NAME" > SHA256SUMS
  sha256sum --check SHA256SUMS >/dev/null
)
printf 'Created %s\nCreated %s\n' "$ARCHIVE" "$CHECKSUMS"
