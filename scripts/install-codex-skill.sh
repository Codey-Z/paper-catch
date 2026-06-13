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

MCP_CONFIG_FOUND=""
for candidate in \
  "$ROOT_DIR/.workbuddy/mcp.json" \
  "${HOME}/.workbuddy/mcp.json" \
  "${HOME}/.config/paper-catch/mcp.json"
do
  if [[ -f "$candidate" ]] && grep -q '"paper-fetch"' "$candidate"; then
    MCP_CONFIG_FOUND="$candidate"
    break
  fi
done

CLI_FOUND=""
if command -v paper-fetch >/dev/null 2>&1; then
  CLI_FOUND="$(command -v paper-fetch)"
fi

if [[ -n "$MCP_CONFIG_FOUND" ]]; then
  echo "Detected paper-fetch MCP config: $MCP_CONFIG_FOUND"
elif [[ -n "$CLI_FOUND" ]]; then
  echo "Verified paper-fetch CLI on PATH: $CLI_FOUND"
else
  echo "Warning: no paper-fetch MCP config or CLI found."
  cat <<'EOF'
Configure at least one paper-fetch backend before running stage 5 downloads.

MCP config is preferred. The pipeline checks these WorkBuddy-compatible files:
  .workbuddy/mcp.json
  ~/.workbuddy/mcp.json
  ~/.config/paper-catch/mcp.json

CLI fallback is also supported.

Recommended: install from upstream Releases:
  https://github.com/Dictation354/paper-fetch-skill/releases

Development/source install:
  mkdir -p external
  git clone https://github.com/Dictation354/paper-fetch-skill.git external/paper-fetch-skill
  cd external/paper-fetch-skill
  ./install.sh --lite
  # Or install into the current Python environment:
  # python3 -m pip install .

Verify:
  .venv/bin/python pipeline/download.py --check-backend --fetch-backend auto
  paper-fetch --help
  paper-fetch --query "10.1186/1471-2105-11-421" --output-dir /tmp/paper-fetch-smoke --artifact-mode none
EOF
fi

ENV_FILE="${HOME}/.config/paper-fetch/.env"
if [[ -f "$ENV_FILE" ]]; then
  echo "Detected paper-fetch env file: $ENV_FILE"
else
  echo "Warning: paper-fetch env file not found: $ENV_FILE"
  cat <<EOF
Create it before paper-fetch downloads:
  mkdir -p "${HOME}/.config/paper-fetch"
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"

Recommended minimum:
  CROSSREF_MAILTO=your-email@example.com

Elsevier full text additionally requires:
  ELSEVIER_API_KEY=...
EOF
fi

echo "Restart Codex to pick up the updated skill."
