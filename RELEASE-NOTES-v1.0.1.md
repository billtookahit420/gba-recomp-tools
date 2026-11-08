# GBARecomp Tools v1.0.1 — release notes (DRAFT — not yet published)

## What was wrong

A conversion subprocess that hangs **silently** — running, printing
nothing, never exiting — could stall a conversion batch forever.
This tool's runner had the worst geometry of the family: it read
the child's output stream to its end *before* starting its wait,
so for a silent child the stream never ended, the wait never
started, and the timeout kill branch that existed in the code was
**unreachable** while the stream stayed open. The queue item sat
there indefinitely: app window open, machine fine, no error, no
progress.

This is the same defect that stalled GBRecomp Tools' GBC run
overnight on **Alone in the Dark – The New Nightmare** and was fixed
there in v1.0.1; the GBA tool shared the vulnerable pattern.

## What v1.0.1 changes

- **Every subprocess now has a real wall-clock timeout** that
  fires even when the child produces no output: the child's output
  is drained on a separate thread while the deadline is enforced
  against the process itself. On timeout the child — **and
  anything it spawned** (its whole process group) — is killed, the
  item fails, and the batch continues with the next game. A failed
  item can no longer sit forever.
- **Timeout error wording changed**: a timeout now reports the
  **full command line** and the kill, e.g.
  `timed out after 3600 s and was killed: /path/gba_recompile --rom …`.
  Previously the message named only the program (`command timed
  out after 3600s: gba_recompile`). If you script against the old
  wording, update your match.
- Preserved exactly as before: the `+ ` log prefix, the 3600 s
  default timeout, and the way a caller's environment is merged
  over the process environment.
- Housekeeping: the engine now carries an explicit version
  constant (`APP_VERSION`), matching the rest of the family.

Nothing else changes. The player-supplied 16 KiB GBA BIOS
requirement is exactly as before — the BIOS is never shipped with
this tool, only prompted for and cached. No framework code was
forked or patched — the fix lives in this tool's engine, and
`gbarecomp` is still downloaded and used as-is.

## Verification (from a clean copy of this exact build)

- **Timeout path, unit level** — a deliberately silent child that
  also spawns a background grandchild is killed at the deadline
  (3.0 s in the test), the whole process group is gone afterwards
  (no orphans), a chatty long-running child is stopped by the same
  clock, and ordinary quick / failing commands behave exactly as
  before. The environment-merge behaviour and the 3600 s default
  are covered by the same tests.
- **Timeout path, batch level** — a fake `gba_recompile` that
  sleeps silently forever fails its queue item in ~5 s with the
  timeout and the full command named; the next item in the batch
  then completes normally.
- **Regression** — Super Mario Advance (USA, Europe), the tool's
  proof title, converts with the fixed engine. _(Clean-room
  conversion evidence is recorded in the fix report; if any part
  could not be re-run on the build VM it is named there rather
  than claimed here. The BIOS is used only inside the test
  environment and is never part of any shipped package.)_

## Still owed (honest list)

- **Publish** — this release is prepared but NOT published; it
  goes live only on Duane's go.
- Everything already owed on Duane's machines is unchanged:
  RetroAchievements login + a real unlock, a connected netplay
  match, and a Windows runtime test.
