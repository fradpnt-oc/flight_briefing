#!/usr/bin/env bash
# PostToolUse hook — auto-format the file that was just written/edited

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[[ -z "$FILE" || ! -f "$FILE" ]] && exit 0

EXT="${FILE##*.}"
BASENAME=$(basename "$FILE")

run_prettier() {
  if command -v prettier &>/dev/null; then
    prettier --write "$FILE" 2>/dev/null
  elif command -v npx &>/dev/null; then
    npx --yes prettier --write "$FILE" 2>/dev/null
  fi
}

case "$BASENAME" in
  docker-compose*.yml|docker-compose*.yaml)
    run_prettier
    exit 0
    ;;
  .env|.env.*)
    # Trim trailing whitespace, remove blank duplicate lines
    python3 -c "
import sys
lines = open('$FILE').readlines()
seen_blank = False
out = []
for l in lines:
    stripped = l.rstrip()
    if stripped == '':
        if not seen_blank:
            out.append('')
            seen_blank = True
    else:
        seen_blank = False
        out.append(stripped)
open('$FILE', 'w').write('\n'.join(out).rstrip() + '\n')
"
    exit 0
    ;;
  Dockerfile|Dockerfile.*)
    # No auto-formatter for Dockerfile; linter handles it
    exit 0
    ;;
esac

case "$EXT" in
  js|jsx|ts|tsx|mjs|cjs)
    run_prettier
    ;;
  html|htm)
    run_prettier
    ;;
  css|scss|less)
    run_prettier
    ;;
  json|jsonc)
    run_prettier
    ;;
  yaml|yml)
    run_prettier
    ;;
  md|markdown)
    run_prettier
    ;;
  py)
    if command -v ruff &>/dev/null; then
      ruff format "$FILE" 2>/dev/null
    elif python3 -m ruff --version &>/dev/null 2>&1; then
      python3 -m ruff format "$FILE" 2>/dev/null
    elif command -v black &>/dev/null; then
      black "$FILE" 2>/dev/null
    elif python3 -m black --version &>/dev/null 2>&1; then
      python3 -m black "$FILE" 2>/dev/null
    fi
    ;;
  sh|bash)
    if command -v shfmt &>/dev/null; then
      shfmt -w "$FILE" 2>/dev/null
    fi
    ;;
esac

exit 0
