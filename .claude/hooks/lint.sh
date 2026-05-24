#!/usr/bin/env bash
# PostToolUse hook — lint + validate the file that was just written/edited

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[[ -z "$FILE" || ! -f "$FILE" ]] && exit 0

EXT="${FILE##*.}"
BASENAME=$(basename "$FILE")

# ── helpers ────────────────────────────────────────────────────────────────

run_eslint() {
  local args=("$FILE")
  if command -v eslint &>/dev/null; then
    eslint --no-eslintrc --env browser,es2021,node --parser-options=ecmaVersion:2021 "${args[@]}" || true
  elif command -v npx &>/dev/null; then
    npx --yes eslint --no-eslintrc --env browser,es2021,node --parser-options=ecmaVersion:2021 "${args[@]}" 2>/dev/null || true
  fi
}

run_stylelint() {
  if command -v stylelint &>/dev/null; then
    stylelint "$FILE" || true
  elif command -v npx &>/dev/null; then
    npx --yes stylelint "$FILE" 2>/dev/null || true
  fi
}

validate_json() {
  python3 -c "
import json, sys
try:
    json.load(open('$FILE'))
except json.JSONDecodeError as e:
    print(f'[validate] $FILE: JSON syntax error: {e}')
    sys.exit(1)
" || true
}

validate_yaml() {
  python3 -c "
import yaml, sys
try:
    list(yaml.safe_load_all(open('$FILE')))
except yaml.YAMLError as e:
    print(f'[validate] $FILE: YAML syntax error: {e}')
    sys.exit(1)
" 2>/dev/null || python3 -c "print('[validate] pyyaml not installed, skipping YAML syntax check')"
}

# ── basename-based rules ───────────────────────────────────────────────────

case "$BASENAME" in
  Dockerfile|Dockerfile.*)
    # Syntax: basic check via hadolint
    if command -v hadolint &>/dev/null; then
      hadolint "$FILE" || true
    fi
    exit 0
    ;;

  docker-compose*.yml|docker-compose*.yaml)
    # Syntax validation
    validate_yaml
    # Schema + service validation via docker compose
    if command -v docker &>/dev/null; then
      docker compose -f "$FILE" config --quiet 2>&1 || true
    fi
    # Lint
    if command -v yamllint &>/dev/null; then
      yamllint -d relaxed "$FILE" || true
    fi
    exit 0
    ;;

  .env|.env.*)
    python3 -c "
issues = []
for i, line in enumerate(open('$FILE'), 1):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if '=' not in line:
        issues.append(f'  line {i}: missing = sign: {line!r}')
    elif line.endswith('='):
        issues.append(f'  line {i}: empty value: {line!r}')
if issues:
    print('[lint] $FILE')
    print('\n'.join(issues))
" || true
    exit 0
    ;;
esac

# ── extension-based rules ──────────────────────────────────────────────────

case "$EXT" in
  js|jsx|mjs|cjs)
    # Syntax validation
    if command -v node &>/dev/null; then
      node --check "$FILE" 2>&1 || true
    fi
    # Lint
    run_eslint
    ;;

  ts|tsx)
    # Type checking (project tsconfig if present, otherwise isolated)
    if command -v tsc &>/dev/null; then
      TSCONFIG=$(find "$(dirname "$FILE")" -maxdepth 3 -name tsconfig.json | head -1)
      if [[ -n "$TSCONFIG" ]]; then
        tsc --noEmit -p "$TSCONFIG" 2>&1 || true
      else
        tsc --noEmit --allowJs --checkJs --strict "$FILE" 2>&1 || true
      fi
    elif command -v npx &>/dev/null; then
      npx --yes tsc --noEmit --allowJs --strict "$FILE" 2>/dev/null || true
    fi
    # Lint
    if command -v eslint &>/dev/null; then
      eslint "$FILE" || true
    elif command -v npx &>/dev/null; then
      npx --yes eslint "$FILE" 2>/dev/null || true
    fi
    ;;

  css|scss|less)
    run_stylelint
    ;;

  py)
    # Syntax validation (always available)
    python3 -m py_compile "$FILE" 2>&1 && echo "[validate] $FILE: syntax OK" || true

    # Type checking
    if command -v mypy &>/dev/null; then
      mypy --ignore-missing-imports "$FILE" || true
    elif python3 -m mypy --version &>/dev/null 2>&1; then
      python3 -m mypy --ignore-missing-imports "$FILE" || true
    elif command -v pyright &>/dev/null; then
      pyright "$FILE" || true
    fi

    # Security scanning
    if command -v bandit &>/dev/null; then
      bandit -q "$FILE" || true
    elif python3 -m bandit --version &>/dev/null 2>&1; then
      python3 -m bandit -q "$FILE" || true
    fi

    # Lint
    if command -v ruff &>/dev/null; then
      ruff check "$FILE" || true
    elif python3 -m ruff --version &>/dev/null 2>&1; then
      python3 -m ruff check "$FILE" || true
    elif command -v flake8 &>/dev/null; then
      flake8 "$FILE" || true
    fi
    ;;

  json|jsonc)
    validate_json
    ;;

  yaml|yml)
    validate_yaml
    if command -v yamllint &>/dev/null; then
      yamllint -d relaxed "$FILE" || true
    fi
    ;;

  sh|bash)
    # Syntax validation
    bash -n "$FILE" 2>&1 || true
    # Lint
    if command -v shellcheck &>/dev/null; then
      shellcheck "$FILE" || true
    fi
    ;;

  html|htm)
    if command -v npx &>/dev/null; then
      npx --yes htmlhint "$FILE" 2>/dev/null || true
    fi
    ;;
esac

exit 0
