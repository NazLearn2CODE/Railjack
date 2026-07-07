---
title: Orbiter Icons
date: 2026-07-07
tags: [orbiter, branding, icons, assets]
---

# Orbiter Icons

Brand source art for the Orbiter application launchers. Palette is locked to the
theme tokens in `web/src/index.css` (vacuum `#070a0f`, signal `#38e0ff`,
phosphor `#cfe3ef`, hazard `#ffb648`, critical `#ff4d6d`) so the icons read as
part of the same product as the console UI.

## Files

| File | Purpose |
|---|---|
| `orbiter-dev.svg` | Start icon — orbital ring + telemetry signal node on the vacuum planet. |
| `orbiter-dev-256.png` / `orbiter-dev-512.png` | Rasterized exports of the start icon. |
| `orbiter-dev-stop.svg` | Stop icon — same mark, critical-red ring + centered halt square. |

## Regenerating the PNGs

```bash
magick -background none -density 384 orbiter-dev.svg orbiter-dev-512.png
magick -background none orbiter-dev-512.png -resize 256x256 orbiter-dev-256.png
```

## Wiring a launcher (per-machine, NOT tracked here)

The actual `.desktop` entries and `~/.local/bin/orbiter-dev*.sh` scripts are
**machine-local** — they live in `~/.local/` on whichever host runs Orbiter, so
each machine's launch paths (project dir, venv) can differ. The office instance
builds its own launcher from this same art.

Install the icon into the user icon theme on a given machine:

```bash
install -d ~/.local/share/icons/hicolor/{48x48,256x256,512x512,scalable}/apps
install -m644 orbiter-dev.svg     ~/.local/share/icons/hicolor/scalable/apps/orbiter-dev.svg
install -m644 orbiter-dev-512.png ~/.local/share/icons/hicolor/512x512/apps/orbiter-dev.png
install -m644 orbiter-dev-256.png ~/.local/share/icons/hicolor/256x256/apps/orbiter-dev.png
```
