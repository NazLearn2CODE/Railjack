#!/bin/sh
# Refresh the gemini/claude telemetry lanes: `agy models` is a pure listing
# (zero LLM quota) but it brings up the Antigravity LanguageServer quota RPC
# for a few seconds — poke the hub while it's up so it harvests + persists
# the reading to ~/.cache/railjack/ag-quota.json.
agy models >/dev/null 2>&1 &
AGY=$!
i=0
while [ $i -lt 10 ]; do
  curl -s --max-time 4 http://127.0.0.1:8700/api/session >/dev/null
  i=$((i+1))
  sleep 2
done
wait $AGY 2>/dev/null
exit 0
