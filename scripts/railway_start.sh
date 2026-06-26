#!/usr/bin/env bash
# Dispatch to the right start command based on which Railway service we're in.
# Both services build from the same Dockerfile, but do different things at runtime.
set -euo pipefail

if [ "${RAILWAY_SERVICE_NAME:-}" = "elephant-cron-tick" ]; then
    # POST to the main service's cron endpoint and exit.
    # Uses Python stdlib so no extra deps are needed.
    python3 - <<'EOF'
import os, sys, urllib.request
secret = os.environ["CRON_SECRET"]
url = os.environ["PUBLIC_URL"].rstrip("/") + "/cron/tick"
req = urllib.request.Request(url, method="POST", headers={"X-Cron-Secret": secret})
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        print(f"cron tick: HTTP {resp.status}")
except urllib.error.HTTPError as e:
    print(f"cron tick failed: HTTP {e.code}", file=sys.stderr)
    sys.exit(1)
EOF
else
    exec fastapi run src/elephant/app.py
fi
