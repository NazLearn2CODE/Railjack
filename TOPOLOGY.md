# Railjack — repo topology

**Two repos, genuinely separate** (split 2026-07-25 out of the old shared
`NazLearn2CODE/SomaticRailjack`, now archived read-only). Each hub lives in its
own single-lineage repo:

- **`NazLearn2CODE/Railjack`** *(this repo)* — the home hub (bazzite, RTX 3090).
  Branch `main`. Local clone `~/Coding Projects/Railjack` (the path has a space).
- **`NazLearn2CODE/Somatic`** — the office hub (Orokin). Branch `main`.
  Local clone `~/Somatic` on the office box.
- **`NazLearn2CODE/SomaticRailjack`** — the old shared repo, **archived**.
  Dropped from the PAT; do not push.

The two lineages have **no common ancestor** — they evolved separately and were
only ever siblings. Hence the golden rule:

> **Differences between the two repos are NOT drift.** Never `diff`, `merge`,
> rebase, or drift-flag across them. Each repo is its own truth for its own
> machine.

## Copy a module across — sibling fetch, then reimplement

Each repo holds the other as a **fetch-only sibling remote** (`somatic` here in
Railjack; `railjack` in Somatic). To take a module from the other side:

```bash
# in THIS repo: stage the office reference just to read it
git fetch somatic && git checkout somatic/main -- app/somemodule.py
# …read it, then write a version native to this repo's paths/config/voice…
git checkout HEAD -- app/somemodule.py   # discard the staged ref — never commit a byte-copy
```

This is a **read-and-adapt** step, not a sync. Per **implement-not-copy**
(`naz-profile.md` § Working Agreements), a cross-repo module move is a
*reimplementation from the reference* — rewrite it native to the target, then
**verify live** (run the suite, fire the route, confirm it serves). The
verify-after is the safety net: a copy that passes mocked tests can still 500
live (the NEWSROOM exec-bit bug was found exactly this way).

## Push rules

- Work in this repo → `git push origin main` (into `Railjack.git`).
- No `machine/*` branches, no shared `main` seed, no `pull.sh`, no `modules/`
  dir — those were the old shared-repo model; the split retired them.

## Don't carry forward the old framing

Pre-2026-07-25 notes may describe "two branches in `SomaticRailjack`, push to
`machine/<host>`, copy across branches, a shared `main` seed." That model is
retired — translate any such handoff to "fetch from the sibling remote,
reimplement from intent, verify live."

See the Cephalon vault note `[[railjack-somatic-topology]]` for the full
rationale and history.
