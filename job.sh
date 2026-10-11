#!/bin/bash
# Finance Desk — one-minute order check (cron).
# Fills open limit and stop-loss orders whose price is met, so they work while the app is closed:
# this Möbius's own paper accounts, and — on the competition host only — every competition player.
set -euo pipefail
APP_ID="$1"
curl -fsS -m 50 -X POST \
  -H "Authorization: Bearer $APP_TOKEN" -H "Content-Type: application/json" -d '{}' \
  "${API_BASE_URL:-http://localhost:8000}/api/apps/$APP_ID/service/jobs/sweep" >/dev/null
