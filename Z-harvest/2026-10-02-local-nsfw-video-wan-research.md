---
title: "Local uncensored video gen — Weerachai FB recipe (character sheets + Wan 2.2)"
date: 2026-10-02
type: harvest
proposed-vault-path: "α-thoughts/research_local_nsfw_video_wan_2026_10_02.md"
source: "Facebook — ComfyUI Thailand group, Weerachai Homjai (2 posts, ~Oct 2026)"
status: "staged-for-vault — Railjack agent has READ-ONLY vault access; Vault Claude or Naz must do the actual vault commit"
tags: [harvest, comfyui, wan, video-gen, local-ai, character-consistency]
---

# Local uncensored video gen — Weerachai FB recipe (harvest draft)

Independent validation (from a Thai local ComfyUI user on 24GB-class hardware) of the
character-sheet → local video pipeline. Two Facebook posts, distilled below.
**Reason for harvest:** the originating chat claimed this was saved to the vault, but a
2026-10-02 vault check found no `α-thoughts/` folder and no matching content — the note
did not survive. This file re-captures everything from the paste.

## Post 1 — "The dream is real"

- URL: https://www.facebook.com/share/p/1EooZUS4wZ/ (ComfyUI Thailand, ~1d before 2026-10-02, 365 👍 / 158 shares)
- Claim (translated): unlimited free video gen, no credits/tokens, runs on local ComfyUI; can make 18+ films.
- Screenshots: NOT the workflow graph — **character reference sheets** for two characters,
  "RAY" and "LIN": full turnarounds, expression grids (neutral/serious/angry/cold/thoughtful…),
  wardrobe + color palettes, key props, height/age/personality specs. I.e. a **cinematic
  character bible** — the exact artifact fed to a reference/omni-based video model to keep
  the same face consistent across shots.
- Model not named in post body (login wall hid the RAM/VRAM answers in the comments).

## Post 2 — "How to make 18+ video with ComfyUI and character sheets" (part 2)

- Same author, ~15h after post 1. 86 reactions, 33 comments. **URL not captured in the
  original paste** — retrieve from the group profile if needed (check for part 3 too).
- His method, in his own words (translated):
  1. Character sheets — can be **combined: multiple characters on ONE page** (RAY+LIN on
     a single 1445×1412 canvas).
  2. Feed sheet + video prompt + motion description + dialogue into ComfyUI.
  3. Wire every node, hit run — clip comes out.
- Screenshots: the **full workflow graph** — prompt/text nodes → big purple prompt-builder
  node with dropdowns → reference injection → sampler chain → video output writing `.mp4`
  (job queue shows completed renders, e.g. `Nikhon_pj1_*.mp4` — multi-part renders
  assembled into longer clips). Plus the character-sheet input node up close (sheet in a
  reference/upload node, `Use Image: True`) next to generation settings:
  **15.0 frames/sample · 2.5s per pass · 880×720**, extended pass-by-pass.

## The distilled recipe

Combined multi-character reference sheet (turnarounds, expressions, wardrobe, props)
+ prompt + motion + dialogue → local ComfyUI → finished uncensored `.mp4`.
No credits, no limits, $0 per render.

## Model verification (standing conclusion from the same-day research)

- **Wan 3.0 / Seedance 2.0 = API-only traps** — not the local lane.
- **Wan 2.2 I2V 14B fp8 = the real lane** on the home RTX 3090 24GB — the leaked specs
  (2.5s/pass @ 880×720, extended per pass) are exactly what a 24GB card does with it.
- Uncensored finetune via Civitai (link referenced in the originating research, not
  included in this paste). Disk cost ~30–60GB. VRAM notes live with that research.

## Home connection (Damriw/Tasai context)

- Home box already has: RTX 3090 24GB ✅, physique files + canon reference renders ✅.
- His character sheets ≈ tidier versions of the existing physique/ref files —
  the missing piece is only the model download + a ComfyUI workflow.
- Next step when triggered: workorder for Tawhan — model download → ComfyUI workflow →
  first test render queued from existing canon refs. `comfyui-media` skill (home-only)
  covers the generate/install lanes.

## Gaps / unknowns (honest list)

1. Model name never confirmed in either post body (login wall hid his spec answers).
2. Post 2 URL + Civitai finetune URL not captured in the paste this file was rebuilt from.
3. Workflow JSON not captured — graph seen only in screenshots.
