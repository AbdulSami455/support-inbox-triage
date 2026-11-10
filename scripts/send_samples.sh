#!/usr/bin/env sh
# Post every sample email to the inbound webhook.
# Usage: WEBHOOK_SECRET=... ./scripts/send_samples.sh [base_url]
set -eu
BASE_URL="${1:-http://localhost:8000}"
: "${WEBHOOK_SECRET:?set WEBHOOK_SECRET (same value as in .env)}"
for f in "$(dirname "$0")"/../samples/emails/*.json; do
  printf '%s -> ' "$(basename "$f")"
  curl -s -X POST "$BASE_URL/api/webhook/email" \
    -H "Content-Type: application/json" -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
    --data @"$f"
  echo
done
