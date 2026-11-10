# GBARecomp Tools v1.0.3 — release notes

## What was wrong

Some games aborted mid-run with a runtime error like:

```
SELF-HEAL bridge for 0x00000170 exceeded 200000000 instructions
without returning to stop_pc=0x082D014A
```

When the recompiler meets code it has no native version of, the
runtime falls back to an interpreter "bridge" that runs a called
subtree until it returns to one pushed return address — with a
200M-instruction cap as the runaway backstop. Two healthy guest
shapes can never satisfy that contract, because their completion
depends on interrupt-driven progress the bridge does not deliver:

- **A routine that never returns by design.** The GBA Video
  carts' player main lives in RAM and is *called* at startup,
  permanently. Inside the bridge it interprets toward a return
  that never comes, and the cap aborts the game — the cart died
  at its logo seconds after boot.
- **A bounded wait on interrupt-advanced state.** Extreme
  Ghostbusters (a regular game cart) runs per-frame audio
  callbacks whose subtree contains a textbook Halt frame-sync
  wait: sleep until a counter changed by the game's interrupt
  handler changes. Inside a bridge, interrupts fire and frames
  tick — measured 2,034 vblanks and 6.25M Halt cycles in one
  bridge — but the waited-on counter never moves, so a healthy
  game starves to the same cap, potentially minutes into a
  session.

Making it permanent, the "self-heal" that should have converted
the affected callbacks to native code **failed to compile** for
exactly this function class: the generated shard referenced
`g_runtime_resume_pc`, which did not exist in the shard's view.
The runtime latched that compile failure and never retried, so
the bridge was the callbacks' only execution mode, every call,
all session.

## What v1.0.3 changes

The engine now applies a small, byte-exact patch to the
framework at conversion time, in the local framework checkout,
right after it is resolved. `gbarecomp` is still downloaded
as-is — the patch is applied locally at conversion time, never
to a fork. The patch:

- **Adds bridge-budget escalation.** When one bridge crosses
  1,000,000 interpreted instructions (and at each later
  multiple), the runtime synchronously heals the bridge's
  entry PC and — when execution has wandered outside the
  entry's region — the *current* PC, compiled as a mid-function
  resume entry. Escalation applies to plain run-to-return
  bridges only; bounded gate probes and IRQ continuations keep
  their old semantics.
- **Transfers at the current PC, frame preserved.** On a landed
  heal the interpreter state is written back exactly as the
  bridge's normal exit writes it, and the current PC is
  dispatched natively with the bridge's call-return frame left
  on the stack. The subtree then runs natively — its wait
  completes under native interrupt semantics — and its eventual
  return unwinds through ordinary native return handling,
  exactly as if the call had been native all along.
- **Makes escalation heal failures non-latching.** A failed
  escalation heal is retried at the next budget multiple
  instead of sentencing the PC to the bridge forever (the
  on-miss background path keeps its existing latch behaviour).
- **Fixes the heal codegen defect at the source.** The overlay
  callback ABI gains the missing resume-PC pointer, so shards
  that reference `g_runtime_resume_pc` compile again.

The 200M cap itself is unchanged and remains the backstop: a
genuine runaway with no interrupt dependence still aborts
loudly. No per-game constants or game-specific branches appear
anywhere in the patch. Patch discipline is the family's usual
one: byte-exact anchors against the pristine framework; an
already-patched tree is a logged no-op; a tree that matches
neither pristine nor patched anchors fails the conversion
loudly instead of shipping an unpatched bridge. Housekeeping:
`APP_VERSION` is now `1.0.3`.

Nothing else changes: the v1.0.1 subprocess timeouts, the
v1.0.2 callable log contract, packaging, and the
player-supplied 16 KiB GBA BIOS requirement are exactly as
before (the BIOS is never shipped with this tool).

## Verification

- **Extreme Ghostbusters — Code Ecto-1: PASS.** Rebuilt with
  the patched runtime: seven bridge escalations across two
  runs, including both fatal shapes from the field crash log
  (same stop_pc `0x082D014A`), transferring at the current PC
  to native code. Zero heal compile failures — previously every
  callback in the subtree failed. Coverage at frame 18,285:
  55 shards healed native; ~15-minute soak; no `crash.log`.
  Unpatched, this session aborts deterministically at the cap.
- **Game Boy Advance Video — Pokémon, Volume 1: PASS.** The
  IWRAM player main was healed by bridge escalation on its
  single bridged call ("HEALED RAM 0x03006D78 -> native via gcc
  … by bridge escalation") and transferred live; the player
  reached its episode menu and **episode video plays**, with
  successive scenes across episodes and frames advancing at
  ≈60 fps past frame 18,000. Under v1.0.2 the same package died
  at the logo.
- **Super Mario Advance (control): PASS, unchanged.** Fresh
  conversion plays title → file select → World 1-1 exactly as
  before, with the healthy coverage shape (59 healed, 0 failed)
  and **zero escalations** — the new machinery never fires for
  a game that never approaches the budget. Ordinary
  bridge-and-return behaviour is confirmed in situ in all three
  titles.

## Caveats (honest list)

- Pokémon's coverage still shows **9 failed heals on the
  background path** — pre-existing RAM-gate latch behaviour,
  unchanged by this release and not on the escalation path;
  bounded RAM helpers still bridge-and-return per call, and
  playback held ≈60 fps regardless.
- Extreme Ghostbusters was verified through its boot/attract
  flow — where the field crash fired and what was soaked — and
  was not driven into a level on the test VM. The framework's
  own `tests/selfheal` / `tests/heal_gate` unit tests were not
  run; heal behaviour is evidenced in-game instead.
- **Existing packages keep the old runtime.** A game converted
  under v1.0.2 (or earlier) gains this fix only when it is
  re-converted / rebuilt under v1.0.3 — the runtime is embedded
  in each package. A conversion run already in progress on
  v1.0.2 is unaffected as a run; any of its titles that hit
  this class can simply be re-run under v1.0.3 later.
- Everything already owed on real hardware is unchanged:
  RetroAchievements login + a real unlock, a connected netplay
  match, and a Windows runtime test.
