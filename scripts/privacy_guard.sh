#!/usr/bin/env bash
# privacy_guard.sh — pre-commit check: blocks secrets and personal data from being committed.
# Never bypass it with --no-verify (project rule).
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
DENYLIST="$ROOT/../private-data/denylist.txt"   # personal word list, lives OUTSIDE the repo
fail=0
report() { echo "  ✗ $1"; fail=1; }

STAGED="$(git diff --cached --name-only --diff-filter=ACMR)"
[ -z "$STAGED" ] && exit 0

# 1) forbidden files by name/path
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    examples/*.csv) ;;                                   # made-up demo data is allowed
    *.csv)                       report "CSV outside examples/: $f" ;;
    .env.example) ;;
    .env|.env.*|*/.env|*/.env.*) report "environment file with secrets: $f" ;;
    *.pem|*.key)                 report "key file: $f" ;;
    *.dump|*.sql.gz|backups/*)   report "database dump/backup: $f" ;;
    notes/*|private-data/*|receipts/*) report "personal folder: $f" ;;
    CLAUDE.md|PLAN.md|PROGRESS.md|START_HERE.md) report "personal workspace file: $f" ;;
  esac
done <<< "$STAGED"

# 2) content of added lines
ADDED="$(git diff --cached -U0 --diff-filter=ACMR | grep -E '^\+[^+]' || true)"

check() { # $1 = description, $2 = regex
  if printf '%s\n' "$ADDED" | grep -Eiq -- "$2"; then report "$1"; fi
}
check "database connection string with a password" 'postgres(ql)?://[^:/ ]+:[^@ ]+@'
check "Anthropic API key"                          'sk-ant-[A-Za-z0-9_-]{10,}'
check "private key"                                'BEGIN [A-Z ]*PRIVATE KEY'
check "real MCP_TOKEN value"                       'MCP_TOKEN *= *["'"'"']?[A-Za-z0-9_-]{20,}'
check "real Bearer token"                          'Bearer +[A-Za-z0-9_-]{24,}'

# 3) personal denylist (if present)
if [ -f "$DENYLIST" ]; then
  WORDS="$(grep -vE '^[[:space:]]*(#|$)' "$DENYLIST" || true)"
  if [ -n "$WORDS" ]; then
    hits="$(printf '%s\n' "$ADDED" | grep -iF -f <(printf '%s\n' "$WORDS") || true)"
    [ -n "$hits" ] && report "word from the personal denylist (lines: $(printf '%s\n' "$hits" | wc -l | tr -d ' '))"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "privacy_guard: commit BLOCKED. Remove these files/lines from the commit:"
  echo "  git restore --staged <file>      (for the very first commit: git rm --cached <file>)"
  exit 1
fi
exit 0
