#!/bin/sh
set -eu
test ! -e /tests/bundle/expected.txt
test -f /app/starter.py
if [ -f /app/require-gpu ]; then nvidia-smi -L; fi
if [ -f /app/require-offline ]; then
python - <<'CHECK'
import socket
try:
    connection = socket.create_connection(("1.1.1.1", 443), timeout=3)
except OSError:
    print("Outbound TCP blocked as required")
else:
    connection.close()
    raise SystemExit("Offline sandbox unexpectedly allowed outbound TCP")
CHECK
fi
mkdir -p /app/submission
printf '42\n' > /app/submission/answer.txt
