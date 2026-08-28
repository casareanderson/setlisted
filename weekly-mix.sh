#!/bin/bash
# Weekly new-music mixes: one for the adults, one for the kids.
# Deterministic — no model in the loop, so it cannot report a playlist it did
# not build. Run it from cron in --no-agent script mode, the same
# shape as any other watchdog job.
#
# The two mixes share one filter (KIDS_MARKERS in spotify_dj.py): for "me" it
# excludes, for "kids" the search terms select and explicit tracks are dropped.
set -uo pipefail
WK=$(date +'%d %b %Y')
DJ="./spotify_dj.py"
rc_total=0

run_mix() {
  local label="$1" name="$2"; shift 2
  local out rc
  out=$($DJ weekly-mix "$name" "$@" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then
    echo "❌ $label FAILED (rc=$rc)"
    echo "$out" | tail -4
    rc_total=1
  else
    echo "🎧 $label"
    echo "$out" | grep -E '^(created|   -|https://)' | head -9
  fi
  echo
}

# No --genres here on purpose: the defaults in spotify_dj.py are the tuned,
# 90s-hip-hop-weighted list. Passing --genres here would silently override them.
run_mix "Weekly Mix — $WK"      "Weekly Mix — $WK"          --audience me   --limit 30
run_mix "Kids Mix — $WK"        "Kids Mix — $WK"     --audience kids --limit 25
exit $rc_total
