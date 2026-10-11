#!/usr/bin/env bash
# Runs the steps in docs/SETUP.md that can run without a Spotify account.
# Used by .github/workflows/setup-check.yml. Run it locally from the repo root too.
set -euo pipefail
step() { printf '\n== %s\n' "$*"; }

step "1. install"
python3 -m pip install -q -r requirements.txt

step "1. offline tests"
out=$(python3 tests/test_sequence.py)
echo "$out" | tail -3
grep -q "ALL OFFLINE TESTS PASS" <<<"$out"

step "1. CLI loads, every subcommand has --help"
python3 spotify_dj.py --help >/dev/null
for c in devices status pause next prev auth-url auth-exchange play volume search queue top \
         weekly-mix new-artists party-set sequence make-playlist; do
  python3 spotify_dj.py "$c" --help >/dev/null || { echo "FAIL: $c --help"; exit 1; }
done
echo "ok"

step "3. no credentials -> clean error, exit 1, no traceback"
tmp=$(mktemp -d)
set +e
err=$(env -u SPOTIFY_CLIENT_ID -u SPOTIFY_CLIENT_SECRET -u SPOTIFY_REFRESH_TOKEN \
      HOME="$tmp" HERMES_AGENT_DIR="$tmp/none" python3 spotify_dj.py auth-url 2>&1)
rc=$?
set -e
echo "$err"
[ "$rc" -eq 1 ] || { echo "FAIL: expected exit 1, got $rc"; exit 1; }
grep -q "^error: SPOTIFY_CLIENT_ID is not set" <<<"$err" || { echo "FAIL: wrong message"; exit 1; }
! grep -q "Traceback" <<<"$err" || { echo "FAIL: traceback"; exit 1; }

step "weekly-mix.sh is valid bash"
bash -n weekly-mix.sh

printf '\nSETUP CHECK PASSED\n'
