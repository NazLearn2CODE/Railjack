# RADIO ▸ News Fill — CHEAP LANE frontend brief (Antigravity)

Add a **cheap lane** to the existing News Fill sub-nav in
`frontend/src/components/NewsroomPanel.tsx`. Two buttons, zero human curation.
Backend is **already shipped, tested (33 pass), and live on `:8700`** — you only build UI.

## What exists (reuse, don't reinvent)
- Doc picker (folder browser) → `selectedDoc: NewsDoc | null` with `{ id, name, kind, date }`,
  `kind` ∈ `"weekday" | "weekend"`.
- Category state → `"global" | "business"` (the existing SCOUT dropdown).
- SCOUT button already injects a command into the ttyd pane via
  `POST /api/terminal/insert` with body `{ text: "/radio-news-scout <category>" }`.
- APPLY already renders a `{ written: [...], skipped: [...] }` result. AUTOPILOT returns the
  **same shape** (+ `auto: true`, `picked: N`) — reuse that renderer verbatim.

## Button 1 — CHEAP SCOUT
Same mechanism as SCOUT, but injects an **exact-count** invocation. Compute the counts from
`category` + `selectedDoc.kind`, then `POST /api/terminal/insert`:

```
text = `/radio-news-scout ${category} --results ${N} --sea ${M} --slice ${K}`
```

Count table (hard-code this map):

| category | kind    | N (results) | M (sea) | K (slice) |
|----------|---------|-------------|---------|-----------|
| global   | weekday | 10          | 3       | 1         |
| global   | weekend | 7           | 2       | 0         |
| business | weekday | 10          | 0       | 0         |
| business | weekend | 6           | 0       | 0         |

- Disabled until a doc is selected (needs `kind`).
- After injecting, the human presses Enter in the pane (same as SCOUT) and waits for the scout to
  finish — no polling needed; AUTOPILOT reads the handoff when clicked.
- Label the button `CHEAP SCOUT`; a sub-caption like `gather exactly ${N} (no review)` is a nice touch.

## Button 2 — AUTOPILOT
One click = convert + place + fill, no ticking. Straight `fetch`:

```
POST /api/newsroom/radio/news/autofill
body: { doc_id: selectedDoc.id, kind: selectedDoc.kind, category }
```

- Disabled until a doc is selected.
- On 200 → render `written[]` / `skipped[]` with the existing APPLY result renderer. Each `written`
  entry now also has `region` (`"SEA"` or `""`) — optionally badge the SEA-led slots.
- On 400/502 → show `detail` (e.g. "no scout handoff yet — run CHEAP SCOUT…", or
  "handoff is 'business' but you asked to fill 'global'…"). These are expected user-facing errors.
- No stdin, no pieces payload — the backend reads `/tmp/railjack-radio-news/latest.json` itself.

## Layout
A small `News Fill · cheap lane` block, ideally next to the existing (curated) SCOUT→CONVERT→tick→APPLY
flow, not replacing it. Both lanes share the doc picker + category. Suggested row:

```
[ CHEAP SCOUT ]  →  [ AUTOPILOT ]
 gather exactly N     convert + place + fill, no hands
```

## Contract notes (don't drift)
- CHEAP SCOUT and AUTOPILOT are a **pair**: cheap gathers exactly the fill count, autopilot places
  all of it. Running AUTOPILOT after a *normal* (15-piece) SCOUT also works — the backend just takes
  the top-N by rule — but the token save comes from CHEAP SCOUT.
- The handoff file is single-category (one `latest.json`); if the user cheap-scouts `global` then
  `business`, the second clobbers the first. AUTOPILOT's category guard catches a mismatch → 400.
- Placement is deterministic server-side (SEA → GLOBAL slot 1 of each broadcast, rest newest-first,
  Slice → AM slot 4 weekday-global). The frontend does **not** decide placement.

## Verify
`tsc && vite build` clean; CHEAP SCOUT injects the right exact-count string for all 4 category/kind
combos; AUTOPILOT posts `{doc_id,kind,category}` and renders the result; both disabled with no doc.
Live end-to-end (real doc) is Naz's manual gate.
