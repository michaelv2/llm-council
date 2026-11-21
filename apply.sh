#!/usr/bin/env bash
# Apply the llm-council-service bundle to a checkout of the llm-council fork.
# Usage: ./apply.sh /path/to/llm-council
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:?usage: apply.sh /path/to/llm-council}"

[ -f "$DEST/backend/council.py" ] || { echo "error: $DEST does not look like the llm-council fork (no backend/council.py)"; exit 1; }
# only tracked modifications block; untracked files (e.g. a local SPEC.md) are fine
if [ -d "$DEST/.git" ] && [ -n "$(git -C "$DEST" status --porcelain --untracked-files=no)" ]; then
  echo "error: $DEST has uncommitted changes to tracked files; commit or stash first"
  exit 1
fi
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" checkout -b service-mode 2>/dev/null || git -C "$DEST" checkout service-mode
  echo "on git branch: $(git -C "$DEST" branch --show-current)"
fi

cp "$SRC/backend/providers.py" "$DEST/backend/providers.py"
cp "$SRC/backend/config.py"    "$DEST/backend/config.py"
cp "$SRC/backend/council.py"   "$DEST/backend/council.py"
cp "$SRC/backend/cli.py"       "$DEST/backend/cli.py"
# service mode replaces the web backend (SPEC.md section 6); frontend/ untouched
rm -f "$DEST/backend/openrouter.py" "$DEST/backend/main.py" "$DEST/backend/storage.py"
cp "$SRC/council.yaml" "$SRC/pyproject.toml" "$DEST/"
cp "$SRC/.env.example" "$DEST/.env.example"
mkdir -p "$DEST/tests"
cp "$SRC/tests/test_service.py" "$DEST/tests/"

echo "Applied to $DEST. Next:"
echo "  cd $DEST"
echo "  cp .env.example .env        # then fill in MISTRAL_API_KEY"
echo "  uv sync"
echo "  uv run python -m pytest tests/test_service.py -q"
echo "  uv run council 'What is 2+2? Reply with just the number.' --json"
echo "  # deploy skills: cp -r $SRC/skills/council-plan $SRC/skills/council-review ~/.vibe/skills/"
