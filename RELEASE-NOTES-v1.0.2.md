# GBARecomp Tools v1.0.2 — release notes (DRAFT — not yet published)

## What was wrong

Every conversion run from the GUI queue failed **instantly**, with
the same error on every title:

```
FAILED <title>: 'builtin_function_or_method' object has no attribute 'write'
```

The engine's log helper expected its log target to be a **file
stream** — something with `.write()` and `.flush()` methods. The
GUI's queue worker passes a **callable** instead (the emit method of
its log signal: it is *called* with each line, it has no `.write`).
The two sides disagreed about the log contract, so the first log
line of the first queued job raised, the worker stamped the job
FAILED, and the queue marched on to fail the next title the same
way. A 1,052-title library run failed 1,052 out of 1,052 this way,
in seconds.

Command-line runs were **not** affected: the CLI entry handed the
engine a real stream (`sys.stdout`), which satisfied the stream
contract — which is also how the mismatch survived every check.
All previous verification of this tool (the Super Mario Advance
proof conversion, the v1.0.1 clean-room regression) drove the
engine through the CLI. The GUI queue path had never run a real
job end-to-end until the 1,052-title run.

Every other tool in the family (GB / SNES / NES) uses the callable
contract in its engine and passes the same kind of callable from
its GUI. The GBA engine was the lone divergence. The bug has been
present since v1.0.0; v1.0.1's timeout fix neither caused it nor
touched the log helper (the helper is byte-identical in the v1.0.0
and v1.0.1 builds).

## What v1.0.2 changes

- **The engine's log helper now uses the family's callable
  contract**: a provided log target is called with the message
  (`log(str(msg))`); with no target, lines print as before. The
  GUI worker's callable now works, and the queue converts again.
- **The CLI entry passes no stream**: it invokes the job runner
  with the default (print) logging, mirroring the sibling tools'
  CLIs, so the command line keeps working under the same contract.
- Housekeeping: `APP_VERSION` moves to 1.0.2, and the README
  version line matches.

Nothing else changes. Framework handling, the v1.0.1 subprocess
timeout behaviour, packaging, and the player-supplied 16 KiB GBA
BIOS requirement are exactly as before — the BIOS is never shipped
with this tool, only prompted for by each converted game's
launcher and cached next to that game. No framework code was
forked or patched — the fix lives in this tool's engine, and
`gbarecomp` is still downloaded and used as-is.

## Verification

- **GUI worker path (the test whose absence shipped the bug)** —
  one real job (Super Mario Advance) driven through the GUI's own
  `Worker` thread class exactly as the app builds it, headless:
  the job runs to **done**, log lines are delivered to the GUI
  signal throughout (framework lines, codegen, build, verify,
  package), and no `AttributeError` occurs. The same job also
  passes from a fresh copy of the shipped tree (clean-room).
  _(The real framework checkout no longer exists on the build VM —
  it was removed in a disk-quota prune after v1.0.1 — so the test
  harness supplies a stub framework checkout and a stub codegen
  binary, and the generated game's build file is replaced with a
  minimal one; everything else — the Worker thread, the engine
  pipeline, cmake configure/build, verify, packaging — is the
  shipped code running for real. The harness is described in the
  fix report.)_
- **CLI path** — the same job through the command-line entry
  completes with `"status": "done"` and correct streamed logging
  (the mirror-image break is checked, not assumed).
- **Unit** — the log helper accepts a plain callable and a bound
  method, coerces non-string messages, and falls back to printing
  when no target is given; the job runner's default logging path
  does not crash.

## Still owed (honest list)

- **Publish** — this release is prepared but NOT published; it
  goes live only on Duane's go.
- Everything already owed on Duane's machines is unchanged:
  RetroAchievements login + a real unlock, a connected netplay
  match, and a Windows runtime test.
