# Module authoring guide

A **module** is one tab on the Railjack dashboard. It either embeds an external
service's web UI (`iframe`) or renders a custom React panel backed by FastAPI
routes (`panel`). Adding one touches **at most three places**, in this order:

1. **Config** — a block under `modules:` in `configs/<machine>.yaml` (always).
2. **Frontend** — a panel component + one line in the `PANELS` map (panel only).
3. **Backend** — an `APIRouter` with `/api/<module>/*` handlers (panel only,
   and only if it needs custom routes).

The pydantic contract for a module is `Module` in `app/config.py`. Read it
before improvising on the field names.

## Decide first: iframe or panel

- **`kind: iframe`** — the service already has a web UI you just want to embed
  (tmux/ttyd, n8n, …). Config-only. Zero frontend, zero backend code.
- **`kind: panel`** — you are building a custom HUD inside Railjack
  (ffmpeg Video Lab, NEWSROOM, …). Needs a frontend component; needs backend
  routes if the panel talks to anything beyond static config.

## Step 1 — Config block

Add a list item under the top-level `modules:` key in this machine's YAML
(`configs/tawhan.yaml` on the home box). The file is selected at boot by
hostname match against `hostnames:`, or forced by the `RAILJACK_CONFIG` env
override — see `app/config.py` `select_config()`.

### Minimal iframe

```yaml
- id: ttyd
  title: TERMINAL
  kind: iframe
  url: http://localhost:7681
  health: { type: http, url: "http://localhost:7681/" }
  manage: { type: systemd-user, unit: ttyd.service }
```

### Minimal panel

```yaml
- id: mytool
  title: MY TOOL
  kind: panel
  panel: mytool        # MUST match the PANELS key in frontend/src/App.tsx
  options:
    output_dir: "~/Downloads/MyTool"
```

### Field reference (`Module`, `app/config.py`)

| Field | Type | When | Notes |
|---|---|---|---|
| `id` | str | always | unique slug; used as the health-map key and the URL hash |
| `title` | str | always | top-bar label |
| `kind` | str | always | `iframe` or `panel` |
| `url` | str | `iframe` | the embedded service URL |
| `panel` | str | `panel` | key into the frontend `PANELS` map (exact string match) |
| `health` | `HealthSpec` | optional | `{ type: http, url }` — probed every 5 s (3 s timeout); 2xx/3xx → `ok`, else `down`. Omit → grey pip. |
| `manage` | `ManageSpec` | optional | start/stop surface for the green/amber button. `type: systemd-user` takes `unit` (+ optional `extra_units`); `type: command` takes `start` / `stop` / `log` argv lists. `start_timeout_s` (default 150) keeps the pip amber after a fire-and-forget start. |
| `options` | dict | optional | free-form, read **server-side** by the backend module from `CONFIG` (see `comfyui.py`/`ffmpeg_jobs.py`/`thailandnow.py`/`notebooklm.py`: `m.options`). **Not** forwarded to the browser — `_sanitize()` omits it, so this is the right place for machine-local paths and keys. |
| `live_dock` | bool | optional | show the always-visible LIVE terminal dock on this tab. Default `false`. A top-level `dock:` block must also exist for the dock to render. |

**Look at real blocks before writing your own:**
- iframe — `tmux`, `n8n` in `configs/tawhan.yaml` (`n8n` also shows
  `live_dock: true` and a same-origin `/n8n/` reverse-proxy URL).
- panel — `ffmpeg`, `newsroom`, `thailandnow` (and `comfyui`, `notebooklm`),
  showing `options:` dicts of increasing richness.

> The hub sanitizes each module before sending it to the browser
> (`_sanitize()` in `app/main.py`) — argv, unit names, probe URLs, **and
> `options`** never leave the server (the browser only gets id/title/kind/url/panel
> + the health/manage/live_dock flags). So `options:` is the *safe* place for
> machine-local paths and keys the backend needs (checkout dir, token paths, …).

## Step 2 — Frontend (panel only)

1. Write `frontend/src/components/<Name>Panel.tsx`. Default-export a React
   component typed `FC<{ module: ModuleConfig }>`:

   ```tsx
   import type { FC } from "react";
   import type { ModuleConfig } from "../store";

   const MyToolPanel: FC<{ module: ModuleConfig }> = ({ module }) => {
     // per-module config lives in YAML `options:` — but the BACKEND reads it;
     // this panel fetches what it needs via /api/<module>/* routes.
     return <section className="hud">…</section>;
   };
   export default MyToolPanel;
   ```

   Per-module config (the YAML `options:` dict) is read **server-side** by the
   backend module — the panel never sees it directly. The panel gets it
   indirectly by calling `/api/<module>/*` routes that return the relevant
   data. Use the repo's `fetchJSON` helper from `../api` (see
   `NewsroomPanel.tsx` for the shape — GET for reads, POST with a JSON body for
   writes).

2. Register it in `frontend/src/App.tsx`:

   ```tsx
   import MyToolPanel from "./components/MyToolPanel";

   const PANELS: Record<string, FC<{ module: ModuleConfig }>> = {
     // …existing entries…
     mytool: MyToolPanel,
   };
   ```

   **The key (`mytool`) must match the YAML `panel:` field character-for-character.**
   A mismatch silently renders nothing — no error, empty tab. This is the
   single most common panel bug.

iframe modules skip this step entirely; `FramePanel` renders the `<iframe>`
from `url` for you.

## Step 3 — Backend routes (panel only, and only if you need custom API)

If your panel needs server-side work (subprocess, file I/O, external API),
add `app/<module>.py`:

```python
from __future__ import annotations
from fastapi import APIRouter, Body, HTTPException

router = APIRouter()

@router.get("/api/mytool/state")
async def get_state() -> dict:
    return {"ok": True}

@router.post("/api/mytool/run")
async def run(body: dict = Body(...)) -> dict:
    if not body.get("text"):
        raise HTTPException(400, "text required")
    return {"done": True}
```

Then wire it into the app in `app/main.py`:

```python
from .mytool import router as mytool_router
# …
app.include_router(mytool_router)
```

**All routes live under `/api/<module>/*`.** Keep it that way — the static
mount that serves the dashboard is a catch-all mounted LAST (`app/main.py`),
so any path outside `/api/*` either collides with a frontend route or hits the
SPA fallback.

`app/newsroom.py` is the canonical in-repo example of this pattern: argv
**lists** via `asyncio.create_subprocess_exec` (never shell), errors surfaced
as `HTTPException` with the script's stderr tail, and a `GET /api/<module>/probe`
the UI can hit for an on-demand health check. Read it before writing your own.

## LLM calls, env vars, and cross-repo moves

- **LLM features route through `app/zai.py`'s `zai_message()`** — never call
  z.ai direct. It posts to the OmniRoute loopback gateway
  (`127.0.0.1:20128`, combo `naz-backup`) which cascades free models past a
  z.ai quota wall. Signature:
  `await zai_message(prompt, max_tokens=400, system=None, model=None, timeout=30.0)`.
  Raises `503` if `OMNIROUTE_API_KEY` is unset, `502` on upstream failure —
  surface both to the panel.
- **Env vars** a backend module may read are documented in `.env.example`
  (root). Add new ones there (blank, commented) so the next machine knows.
- **Cross-repo moves** (home `Railjack` ↔ office `Somatic`) are a
  reimplementation, not a copy. See `TOPOLOGY.md` — fetch the sibling remote
  read-only, rewrite native to the target repo's paths and voice, then verify
  live. Never byte-copy; the two repos have no common ancestor.

## Verify-after — non-negotiable

A module that compiles can still 500 live. Run all three before you call it done:

1. **Rebuild the frontend** (panel only — iframe modules skip this):

   ```bash
   cd frontend && npm run build
   ```

2. **Pick up the config change without a full restart:**

   ```bash
   curl -X POST http://localhost:8700/api/config/reload
   ```

   On a broken YAML this returns `400` and leaves the running config intact —
   if you see it, fix the YAML and re-POST. (Fallback: `systemctl --user restart railjack`.)

3. **Confirm it serves live:**

   ```bash
   # health map includes your module id → "ok" or "down" (if it has a health spec)
   curl -s localhost:8700/api/health | jq .
   # fire one of your own routes (panel + backend only)
   curl -s localhost:8700/api/<module>/<route> | jq .
   ```

   Then open `http://localhost:8700/#<module-id>` in the browser and exercise
   the panel. A `200` on the API with the data you expected is the only
   passing grade — the live reload + one real route call is the safety net
   that catches the exec-bit, missing-cred, and stale-config classes of bug.
