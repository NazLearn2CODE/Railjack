# SomaticRailjack — repo topology

One repo (`github.com/NazLearn2CODE/SomaticRailjack`), **two independent hub
lineages as branches**. Naz develops both hubs himself — home (Railjack) and
office (Somatic). They share code by **copying modules across on demand**,
not by syncing against a shared trunk.

## Branches

| Branch | Machine | What |
|---|---|---|
| `machine/railjack` | home (`bazzite`) | Railjack hub — ComfyUI/RTX 3090, Video Lab, NEWSROOM, RESEARCH |
| `machine/somatic` | office (`Orokin`) | Somatic hub — news-production box |
| `main` | — | **abandoned seed (`169aaa5`), ignore.** Never push here. |

The two machine branches have **no common ancestor** — fully independent
histories. That's intentional, not drift.

## Copying a module across

```bash
# pull one file/module from the sibling branch into your working tree, review, commit
git checkout somatic/machine/somatic -- app/newsroom.py
```

Then **verify live**: run the suite, fire the route, confirm it serves. (That's
how a latent exec-bit bug surfaced — the copy passed mocked tests but 500'd
live. Verify-after-copy is the safety net, not the copy method.)

## Push rules

- Home work → `git push somatic HEAD:machine/railjack`
- Office work → `git push somatic HEAD:machine/somatic`
- **Never push to `main`.** It's the abandoned seed.

## What's retired

The shared-`main` + `pull.sh` + `modules/` layout (seed `169aaa5`) was an
abandoned migration — only Somatic ever adopted it, never home, so the
histories went disjoint. Rather than finish a multi-hour migration, we keep the
two-branch copy-across model: it matches how the hubs genuinely differ, and for
two personally-developed boxes a manual copy is less machinery than a sync tool.

`main`, `pull.sh`, and `modules/` are vestigial — leave them be; deleting is
optional and low-value. Older notes that say "adopt via pull.sh" are stale;
read them as "copy across + verify."

See the Cephalon vault note `[[railjack-somatic-topology]]` for the full
rationale and history.
