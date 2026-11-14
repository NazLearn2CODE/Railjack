"""Single source of truth for the Antigravity CLI (agy) model id.

AI companies swap model generations often (Naz, 2026-11-11); every Railjack
lane that talks to agy reads the id from THIS constant, so a generation swap
is a one-line change here. Current generation: Gemini 4 Hydrogen (replaced
Gemini 3.8 Flash, 2026-11-11).

BARE id on purpose — no -high/-medium/-low effort suffix: Railjack passes
effort separately (`--effort` flag / `effort=` arg), and agy 1.5.x REJECTS a
suffixed id combined with an effort flag ("--model gemini-4-hydrogen-high
--effort low: conflicts with --effort=low"; live-probed 2026-11-11). Keep the
effort in the call sites. Verify a new id with:
`agy -p "Reply with exactly: OK" --model <id> --effort low`
"""

AGY_MODEL = "gemini-4-hydrogen"
