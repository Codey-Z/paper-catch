#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install the auto-paper-pipeline Codex skill.

Usage:
  ./scripts/install-codex-skill.sh [--project]

Options:
  --project   Install into the repository-local .codex/skills directory.
  --help      Show this help message.
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/skills/auto-paper-pipeline"
INSTALL_SCOPE="global"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      INSTALL_SCOPE="project"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$SOURCE_DIR/SKILL.md" ]]; then
  echo "Skill source not found: $SOURCE_DIR/SKILL.md" >&2
  exit 1
fi

if [[ "$INSTALL_SCOPE" == "project" ]]; then
  DEST_DIR="$ROOT_DIR/.codex/skills/auto-paper-pipeline"
else
  CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
  DEST_DIR="$CODEX_HOME_DIR/skills/auto-paper-pipeline"
fi

TMP_DIR="${DEST_DIR}.tmp"
mkdir -p "$(dirname "$DEST_DIR")"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"
cp "$SOURCE_DIR/SKILL.md" "$TMP_DIR/SKILL.md"
rm -rf "$DEST_DIR"
mv "$TMP_DIR" "$DEST_DIR"

echo "Installed auto-paper-pipeline to: $DEST_DIR"

if command -v paper-fetch >/dev/null 2>&1; then
  echo "Verified paper-fetch CLI on PATH: $(command -v paper-fetch)"
else
  echo "Warning: paper-fetch CLI not found on PATH."
  echo "Install Dictation354/paper-fetch-skill first:"
  echo "  https://github.com/Dictation354/paper-fetch-skill"
fi

ENV_FILE="${HOME}/.config/paper-fetch/.env"
if [[ -f "$ENV_FILE" ]]; then
  echo "Detected paper-fetch env file: $ENV_FILE"
else
  echo "Warning: paper-fetch env file not found: $ENV_FILE"
  echo "Copy .env.example there if you have not configured it yet."
fi

echo "Restart Codex to pick up the updated skill."
