#!/bin/bash
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

SRC="${CLAUDE_PROJECT_DIR:-$(pwd)}/marketing-skills/skills"
DEST="$HOME/.claude/skills"

if [ ! -d "$SRC" ]; then
  echo "marketing-skills/skills not found at $SRC" >&2
  exit 1
fi

mkdir -p "$DEST"
cp -r "$SRC"/* "$DEST"/

echo "Installed $(ls -1 "$SRC" | wc -l) marketing skills into $DEST"
