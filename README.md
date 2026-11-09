# GBARecomp Tools

**Version 1.0.2** — GUI queue runs work again: the engine's log
helper now takes the same callable log target the GUI passes (it
previously expected a file stream, so every queued job failed
instantly); see RELEASE-NOTES-v1.0.2.md.

Convert your own Game Boy Advance ROM dumps into native PC games —
no emulator: each game's ARM code is statically recompiled to a
real executable with a launcher, box art, RetroAchievements and
link-cable play.

Built on **gbarecomp** by **Matthew Stanley (mstan)** /
**RetroPortingToolkit** (https://retroportingtoolkit.com —
github.com/mstan, github.com/RetroPortingToolKit). Framework
license: PolyForm Noncommercial 1.0.0. Launcher: **recomp-ui**
(MIT, © 2026 Matthew Stanley).

## Run it

```sh
./gba-recomp-tools.sh
```

First run creates a Python venv and installs PySide6 + Pillow.
Add `.gba` files (or a folder), then **Run Queue**. Each game is
probed from its cartridge header, converted, and packaged as
`<Game> (GBA) - linux.zip` / `<Game> (GBA) - windows.zip` in the
output folder. One target failing never sinks the other — the row
says which package you got.

CLI: `./gba-recomp-tools.sh --cli toolbox|probe <rom>|run --job <job.json>`

## What a converted game needs

- **Your own ROM dump** of the exact cartridge — the package does
  not contain it; the launcher asks once and verifies its SHA-1.
- **Your own GBA BIOS dump** (16 KiB, usually `gba_bios.bin`) —
  the GBA firmware is copyrighted, so no package can include it.
  The launcher asks on first run and remembers (`bios.cfg`).
  There is no BIOS-free mode for GBA.
- Keep the `assets/` folder next to the executable.

## Features

- **RetroAchievements** (on by default): login toast + achievement
  pop-ups in game. Put your filled `retroachievements.toml` in
  Settings (username + the Connect API token from
  retroachievements.org). RA counts hardcore only from approved
  clients, so recomp unlocks register as Casual/softcore — that's
  an RA policy gate, not a bug.
- **Cover art** from libretro-thumbnails
  (Nintendo - Game Boy Advance), baked into the launcher and
  included under `artwork/` in each package.
- **Link play**: same build + same verified ROM on both sides,
  from the launcher's NETPLAY screen (LAN lobbies / direct IP).
  *Status: the full lobby flow — NETPLAY button, mode picker,
  LAN lobby list, host dialog with the game's player cap, lobby
  creation, seating, chat, match settings — is verified working
  in the shipped build. A connected match has not yet been
  demonstrated from the converter's build sandbox: its firewall
  blocks the session traffic (UDP), and its only BIOS is an
  open-source replacement whose SHA-1 the launcher's bootability
  gate rejects for lobby play (it expects the retail dump).
  Test a real match on your own machines with your retail BIOS
  dump.*

## Probe / "needs review"

The probe mirrors the framework's own identification: header
title/game code, the complement check over 0xA0–0xBC, the logo
sanity check, save-type signatures (EEPROM / SRAM / Flash), and
the runtime's hard 32 MiB cartridge limit. ROMs are flagged for
review when they exceed that limit (the 64 MB GBA Video carts),
fail the header checks, have a non-standard entry point, or are
Game Boy Advance Video titles (a dedicated playback path the
runtime doesn't special-case). A passing probe is not a guarantee
any given game converts perfectly — it's a header-level check.

Census of the reference 1,052-ROM collection: 1,019 pass clean,
33 flagged (30 GBA Video titles; 3 unlicensed/prototype images
whose logo area fails the sanity check). Save types: EEPROM ×531,
none ×398, SRAM ×76, Flash512 ×35, Flash1M ×12.

## Windows builds

Windows packages are cross-built with MinGW. The app owns its
Windows support files (SDL2 MinGW development kit) in its own
`tools/` folder and downloads them on the first Windows build —
nothing is borrowed from any other project's folders, and every
file is validated individually before a build starts.
