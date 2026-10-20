#!/usr/bin/env bash
# Scan this repository -- working tree AND full git history -- for credentials
# and personal data. Run it before publishing.
#
#   ./scripts/scan-secrets.sh            # scan
#   ./scripts/scan-secrets.sh --verify   # scan, and prove the scanner still works
#
# Exit 0 = clean, exit 1 = something needs a human look.
# tests/test_selfscan.py runs equivalent checks inside the test suite; this
# script exists so a human can run it standalone, on any clone.
#
# Design notes, because a scanner that silently scans nothing is worse than no
# scanner at all:
#   * Patterns live in a file and go through `grep -f`. With multiple positional
#     patterns, grep reads the later filenames as more patterns, matches
#     nothing, and exits 0.
#   * File lists go through `xargs -0 grep -f`, never a shell function: an
#     unexported function is invisible to xargs (127), which also looks clean.
#   * --verify plants a fake secret and fails unless this scanner catches it,
#     so the scanner cannot rot into a no-op.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2

PATTERNS="$(mktemp)"
PERSONAL="$(mktemp)"
trap 'rm -f "$PATTERNS" "$PERSONAL"' EXIT

# Real key families with long tails. Not anchored with ^: in real code a key
# sits after `KEY = ` or inside a dict, and anchoring to line start would miss
# exactly the case that matters. The length floors keep prose like
# "sk-or-v1-<32 chars>" in documentation from tripping it.
cat > "$PATTERNS" <<'EOF'
sk-ant-(api|oat|usr|svc|admin)[A-Za-z0-9_-]{20,}
sk-or-v1-[A-Za-z0-9]{32,}
sk-cp-[A-Za-z0-9]{24,}
gh[pousr]_[A-Za-z0-9]{30,}
github_pat_[A-Za-z0-9_]{20,}
sk-(proj-)?[A-Za-z0-9]{40,}
eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
EOF

cat > "$PERSONAL" <<'EOF'
[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}
/home/[a-z][a-z0-9_-]{2,}
C:\\Users\\[A-Za-z]
EOF

fail=0
report() { printf '\n=== %s ===\n' "$1"; }
flag()  { printf '  [HIT] %s\n' "$1"; fail=1; }

# Files where a token-shaped string is expected and safe (the patterns
# themselves, and tests that prove the redaction works).
SELF_ALLOW='^(quota_rings/secrets\.py|scripts/scan-secrets\.sh|tests/test_selfscan\.py|tests/test_collector\.py|tests/test_server\.py)'

report "working tree: vendor tokens"
if git ls-files -z 2>/dev/null | xargs -0 -r grep -nI -E -f "$PATTERNS" 2>/dev/null \
   | grep -vE "$SELF_ALLOW"; then
  flag "a live-looking vendor token is committed"
fi

report "working tree: personal data"
if git ls-files -z 2>/dev/null | xargs -0 -r grep -nI -E -f "$PERSONAL" 2>/dev/null \
   | grep -vE 'example\.|noreply'; then
  flag "an email address or a personal absolute path is committed"
fi

report "config files: literal credentials"
if git ls-files -z '*.toml' 2>/dev/null | xargs -0 -r grep -nI -E \
     '^[[:space:]]*(token|management_token)[[:space:]]*=[[:space:]]*"[^"]' 2>/dev/null \
   | grep -vE '"(env|file|command):'; then
  flag "a config holds a literal credential instead of a reference"
fi

report "untracked files that are not ignored"
untracked="$(git ls-files --others --exclude-standard 2>/dev/null)"
if [ -n "$untracked" ]; then
  printf '%s\n' "$untracked" | sed 's/^/  [UNTRACKED] /'
  flag "an untracked file exists: add it to .gitignore or commit it deliberately"
fi

# ---------------------------------------------------------------------- history
if git rev-parse --git-dir >/dev/null 2>&1; then
  report "git history: vendor tokens and personal paths"
  history="$(git log -p --all 2>/dev/null)"
  if [ -n "$history" ]; then
    # `git log -p` content lines carry no filename prefix, so the allowlist has
    # to be applied per patch: track the file from each "diff --git a/x b/x"
    # header and drop that patch when it is the scanner itself or a redaction
    # test. Commit headers are dropped too, because the author/committer
    # identity lives there and is reported in full by the section below --
    # scanning it here would double-report the same address under a rule meant
    # for file content.
    leaked="$(
      awk -v allow="$SELF_ALLOW" '
        /^diff --git / { path = $0; sub(/.* b\//, "", path); skip = (path ~ allow) }
        /^(Author|AuthorDate|Commit|CommitDate|Date): / { next }
        !skip
      ' <<< "$history" \
      | { grep -aE -f "$PATTERNS" || true
          grep -aE -f "$PERSONAL" | grep -vE 'example\.|noreply' || true; } \
      | head -20
    )"
    if [ -n "$leaked" ]; then
      printf '%s\n' "$leaked" | sed 's/^/  /'
      flag "the history contains a credential or a personal path -- do NOT publish as-is"
    else
      echo "  clean"
    fi
  else
    echo "  no commits yet"
  fi

  report "git history: commit identity (metadata -- review before publishing)"
  git log --all --format='  author:    %an <%ae>%n  committer: %cn <%ce>' 2>/dev/null | sort -u
fi

# -------------------------------------------------------------------- binaries
report "binary assets: embedded metadata"
python3 - "$REPO" <<'PY'
import struct, sys
from pathlib import Path
root = Path(sys.argv[1])
bad = []
pngs = [p for p in sorted(root.rglob("*.png"))
        if not any(part in {".git", "__pycache__", ".venv"} for part in p.parts)]
if not pngs:
    print("  no PNG assets")
for png in pngs:
    data = png.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        bad.append(f"{png}: not a PNG")
        continue
    i, kinds = 8, []
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8].decode("latin1")
        kinds.append(kind)
        if kind in {"tEXt", "iTXt", "zTXt", "eXIf"}:
            bad.append(f"{png.relative_to(root)}: carries a {kind} metadata chunk")
        i += 12 + length
    print(f"  {png.relative_to(root)}: {' '.join(sorted(set(kinds)))}")
for problem in bad:
    print(f"  [HIT] {problem}")
sys.exit(1 if bad else 0)
PY
[ $? -ne 0 ] && flag "a binary asset carries embedded metadata"

# ---------------------------------------------------------------- verification
if [ "${1:-}" = "--verify" ]; then
  report "self-verification: can this scanner still detect a leak?"
  probe="quota_rings/_scan_selfcheck.py"
  printf 'LEAK = "sk-or-v1-%s"\n' "$(printf 'a%.0s' $(seq 48))" > "$probe"
  git add -f "$probe" 2>/dev/null
  if git ls-files -z 2>/dev/null | xargs -0 -r grep -nI -E -f "$PATTERNS" 2>/dev/null \
       | grep -q "$probe"; then
    echo "  PASS: a planted vendor key was detected"
  else
    echo "  FAIL: a planted vendor key went undetected -- the scan is broken"
    fail=1
  fi
  git rm -q --cached "$probe" 2>/dev/null
  rm -f "$probe"
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "RESULT: clean."
  exit 0
fi
echo "RESULT: review the [HIT] lines above before publishing."
exit 1
