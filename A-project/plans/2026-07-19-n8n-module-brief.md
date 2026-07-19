# N8N module for home Railjack — build brief (self-contained)

**Status: PHASE A DONE · PHASE B DONE · Phase C helpers pre-written (inert) — BLOCKED on Naz's manual browser step (C.1: owner account + API key).** Verify A + B green; Verify C NOT-VERIFIED (needs the key). Next: Naz does C.1 in a browser, then any agent runs Verify C and proceeds to Phase D. If you are a fresh agent picking this up cold: this file is the whole spec — read it top to bottom, then check § Progress log at the bottom for where the last agent stopped.

## Context (why / who)

- **Machine:** home box "Tawhan" (`hostname -s` = `bazzite`, Bazzite/atomic,
  rootless Podman 5.8.4, user linger ON). User: `NAZ`, home `/var/home/NAZ`.
- **Repo:** `~/Coding Projects/Railjack` — the home hub (FastAPI `app/` +
  React `frontend/`, config-driven modules via `configs/tawhan.yaml`).
  Runs as systemd user service on `:8700`.
- **Goal:** add an **N8N module** to home Railjack, mirroring Tasai's Somatic
  setup on Orokin (vault note: `~/Cephalon/10-knowledge/hermes/n8n-hermes-bridge.md`)
  **minus the entire Hermes/Damriw bridge** — no webhook platform, no HMAC
  bridge, no guardrail work. We (Naz + Tawhan) use n8n **directly**: UI in the
  Railjack iframe, REST API from Claude's Bash.
- **Division of labor:** Tawhan (Claude, Fable/Sonnet) planned this; GLM-5
  builds it; verification per phase below. Naz is a non-coder — verify by
  RUNNING things, never by diff-reading. Git checkpoint before each phase.

## Phase A — n8n infra (host-level, no repo changes)

Copy Tasai's proven Quadlet pattern (all values here are the home-box versions):

1. Dirs: `mkdir -p ~/n8n/data`.
2. `~/n8n/n8n.env` (non-secret):
   ```
   N8N_HOST=127.0.0.1
   N8N_PORT=5678
   N8N_PROTOCOL=http
   GENERIC_TIMEZONE=Asia/Bangkok
   TZ=Asia/Bangkok
   ```
3. `~/n8n/.secrets.env` (chmod 600): `N8N_ENCRYPTION_KEY=<openssl rand -hex 32>`.
   Never commit; never echo into the terminal transcript more than needed.
4. Quadlet unit `~/.config/containers/systemd/n8n.container`:
   ```ini
   [Unit]
   Description=n8n workflow automation (rootless)
   Wants=network-online.target
   After=network-online.target

   [Container]
   Image=docker.io/n8nio/n8n:2.29.8
   ContainerName=n8n
   PublishPort=127.0.0.1:5678:5678
   Volume=/var/home/NAZ/n8n/data:/home/node/.n8n:Z
   EnvironmentFile=/var/home/NAZ/n8n/n8n.env
   EnvironmentFile=/var/home/NAZ/n8n/.secrets.env
   Environment=N8N_BLOCK_ENV_ACCESS_IN_NODE=false
   Environment=NODE_FUNCTION_ALLOW_ENV=*
   Environment=NODE_FUNCTION_ALLOW_BUILTIN=crypto
   Environment=N8N_RUNNERS_ENABLED=true

   [Service]
   Restart=on-failure

   [Install]
   WantedBy=default.target
   ```
   Notes: the three `NODE_FUNCTION`/`BLOCK_ENV` lines are required as direct
   `Environment=` lines (EnvironmentFile alone doesn't reach Code-node sandbox
   — Orokin-proven). **No** `Network=pasta:--map-host-loopback` line — that hop
   only existed for the Hermes bridge; we don't need container→host-loopback.
5. Known gotcha (hit on Orokin): first start may die `EACCES /home/node/.n8n/config`
   → fix: `podman unshare chown -R 1000:1000 /var/home/NAZ/n8n/data` and restart.
6. Start: `systemctl --user daemon-reload && systemctl --user start n8n`.

**Verify A:** `systemctl --user status n8n` active; `curl -s http://127.0.0.1:5678/healthz`
returns ok; from another device (or `curl` against the LAN IP) port 5678 REFUSES
(loopback-only). `journalctl --user -u n8n -e` clean of errors.

## Phase B — Railjack module block (repo change, one block)

Branch first: `git checkout -b feat/n8n-module` in `~/Coding Projects/Railjack`.

Append to `modules:` in `configs/tawhan.yaml` (mirror the `tmux` block's shape —
home schema uses structured `health:`/`manage:`, NOT Orokin's flat keys):

```yaml
  - id: n8n
    title: N8N
    kind: iframe
    url: http://127.0.0.1:5678/
    health: { type: http, url: "http://127.0.0.1:5678/healthz" }
    manage: { type: systemd-user, unit: n8n.service }
```

Zero core-code change expected — the hub is config-driven (iframes render once
in `FramePanel.tsx`, toggled via CSS display; health pips poll `/api/health`).
If `app/config.py` or `app/health.py`/`app/manage.py` validation rejects the
block, fix minimally and add a test mirroring the existing module tests in
`app/tests/`.

**Verify B:** restart the Railjack service (find the unit:
`systemctl --user list-units | grep -i railjack` — likely `railjack.service`);
open `http://localhost:8700` → N8N appears in the module rail with a **green
pip**, clicking it shows the n8n UI in the frame, and switching modules
back/forth does NOT reload the terminal (the render-once rule). Run the
existing test suite (`cd app && python -m pytest` or however `app/tests` runs)
— all green.

## Phase C — direct-use tooling (no Hermes)

1. **Naz manual step (browser, cannot be automated, ttyd blocks paste):**
   open `http://localhost:5678`, create the owner account, then Settings →
   API → create key. Put it in `~/n8n/.secrets.env` as `N8N_API_KEY=...`
   via a GUI editor.
2. Helper script `~/n8n/trigger.sh` (adapt Tasai's `tasai-trigger.sh`):
   `trigger.sh [--test] <webhook-path> '<json>'` → POST
   `http://localhost:5678/webhook[-test]/<path>`. Webhook-trigger nodes need
   no API key.
3. Sanity helper `~/n8n/api.sh`: wraps `curl -H "X-N8N-API-KEY: $N8N_API_KEY"`
   against `/api/v1/*` (sources `.secrets.env`).
4. Known API gotchas (Orokin-proven, keep for reference): manual-trigger
   workflows return **405** via REST `POST /workflows/{id}/run` — run headless
   with `podman exec -e N8N_RUNNERS_TASK_BROKER_PORT=5690 -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_RUNNERS_BROKER_LISTEN_ADDRESS=127.0.0.1 n8n n8n execute --id <wf>`.

**Verify C:** `api.sh` → `GET /api/v1/workflows` returns 200 (after Naz mints
the key); `trigger.sh --test` round-trips against a scratch Webhook workflow.

## Phase D — wrap-up

- Commit the repo change on `feat/n8n-module`, merge to local `main`, push to
  the shared remote **backup branch only**: `git push somatic main:machine/railjack`
  (NEVER shared `main` — 2026-07-18 force-push incident rule).
- Update `~/Cephalon/20-projects/railjack.md` (n8n module row) + one-line
  `logs/memory-log.md` entry + hot.md note to Tasai (Tawhan does this part if
  alive; else leave a note in § Progress log and Naz/next session handles the
  vault — **vault writes are Cephalon-session work, not the GLM child's**).

## Hard rules for the builder

- Do NOT touch the Hermes/webhook config anywhere. No `~/.hermes`, no HMAC.
- Do NOT push to the shared repo's `main`. Backup branch only.
- Loopback binds only (`127.0.0.1`) — home firewall posture unverified.
- Secrets live in `~/n8n/.secrets.env` (600); never in the repo or vault.
- Frontend: if any UI tweak is needed, follow the design language
  (`~/Cephalon/20-projects/railjack-design-language.md`) — but an iframe
  module should need zero frontend edits.
- Verify by running (curl/pytest/browser), then report VERIFIED/NOT-VERIFIED
  per phase honestly.

## Progress log (append here, newest last)

- 2026-07-19 Tawhan (Fable): brief written; nothing built yet.
- 2026-07-19 GLM-5 (builder): **Phase A DONE — VERIFIED.** Dirs `~/n8n/data` +
  quadlet dir created. `n8n.env`, `.secrets.env` (600, 84 bytes = key-prefix +
  64 hex, value never echoed), and `~/.config/containers/systemd/n8n.container`
  written verbatim per brief (no pasta host-loopback line). First start hit the
  documented Orokin gotcha — `EACCES: permission denied, open '/home/node/.n8n/config'`
  — fixed exactly per step 5: `podman unshare chown -R 1000:1000
  /var/home/NAZ/n8n/data` + restart. Evidence: `systemctl --user is-active
  n8n.service` = `active`; `curl 127.0.0.1:5678/healthz` = `{"status":"ok"}`
  held across 20 consecutive 3s polls (60s stable); loopback-only — `curl
  192.168.1.109:5678` → `Connection refused` and `ss -tlnp | grep 5678` shows
  `LISTEN 127.0.0.1:5678` (pasta bound to loopback, not 0.0.0.0); journal
  post-fix clean of errors (only benign DB-migration step names), tail ends
  `Editor is now accessible via: http://127.0.0.1:5678`.
- 2026-07-19 GLM-5 (builder): **Phase B DONE — VERIFIED (render-once
  code-verified; browser click-through deferred to Naz — no browser automation
  available here and ttyd blocks paste).** Branch `feat/n8n-module` cut from
  `main`. n8n block appended to `modules:` in `configs/tawhan.yaml` mirroring
  the tmux shape. Pydantic `MachineConfig` accepted it unchanged — **zero
  core-code change**, as predicted. Evidence: `select_config` modules list =
  `['tmux','comfyui','ffmpeg','notebooklm','n8n']`; after `systemctl --user
  restart railjack.service`, `/api/health` = `{"tmux":"ok","comfyui":"down",
  "n8n":"ok"}` (comfyui down = expected, not running; ffmpeg/notebooklm absent
  = panels with no health spec — correct); `/api/config` serves n8n as
  `{kind:iframe, url:http://127.0.0.1:5678/}` to the frontend; n8n UI serves
  real HTML at the iframe url (HTTP 200, text/html, `<title>n8n.io - Workflow
  Automation</title>`); render-once confirmed in `frontend/src/components/
  FramePanel.tsx` (line 9 filters all `kind==="iframe"`, line 25 `.map()`
  renders each once, line 31 `display: activeId===m.id?"block":"none"` toggles
  via CSS → switching modules never remounts any iframe, terminal included);
  `.venv/bin/pytest -q` = **154 passed** (1 pre-existing Starlette/httpx
  deprecation warning, unrelated). NOTE: brief's `app/tests/` path is actually
  `./tests/` at repo root — tests found and green there. Local commit
  `6d920b5` on `feat/n8n-module` (NO push — Phase D's job, shared-main push
  forbidden).
- 2026-07-19 GLM-5 (builder): **Phase C helpers PRE-WRITTEN, inert.** Wrote
  `~/n8n/trigger.sh` (`--test` toggle, POST to `/webhook[-test]/<path>`, no key
  needed) and `~/n8n/api.sh` (sources `~/n8n/.secrets.env`, wraps
  `curl -H "X-N8N-API-KEY: $N8N_API_KEY"` against `/api/v1/*`); both `chmod +x`,
  loopback-only (`http://127.0.0.1:5678`), arg-usage guards. Self-checks (run,
  not diff-read): trigger.sh no-arg → exit 2 + usage; trigger.sh probe POST to
  `/__naz_probe__` → n8n answered **404** (webhook router live, URL constructed
  correctly — curl's real exit was 22, the `head`-pipe `$?` read 0, cosmetic
  only); api.sh missing-key guard fires → `N8N_API_KEY not set` exit 2 (correct
  — `.secrets.env` has only the encryption key so far). **STOPPED before Phase
  C step 1 as instructed:** did NOT create the n8n owner account and did NOT
  mint the API key (Naz's manual browser step; ttyd blocks paste). **Verify C
  = NOT-VERIFIED, blocked on Naz** — `api.sh GET /workflows` 200 and
  `trigger.sh --test` round-trip both need the key + a scratch webhook workflow,
  which only exist after Naz's manual step. BLOCKER for next agent/Naz: (1) do
  Phase C step 1 in a browser at `http://localhost:5678`, (2) append
  `N8N_API_KEY=...` to `~/n8n/.secrets.env` (mode 600), (3) run Verify C.
