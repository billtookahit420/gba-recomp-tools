#!/usr/bin/env python3
"""GBARecomp Tools engine - UI-free core for the GBA recomp converter.

Converts Game Boy Advance ROMs (.gba) into native PC builds using
mstan's gbarecomp static-recompilation framework
(github.com/mstan/gbarecomp, part of RetroPortingToolkit — PolyForm
Noncommercial 1.0.0), packaged with the recomp-ui launcher
(MIT, (c) 2026 Matthew Stanley).

Pipeline: probe -> resolve framework (clone+build if missing) ->
scaffold (game.toml + generic host from the templates below) ->
gba_recompile code generation -> CMake build (Linux + Windows/MinGW)
-> verify -> package. The GUI is a thin shell over run_job(); there is
also a CLI:

    engine.py probe <rom>
    engine.py toolbox
    engine.py run --job job.json

Job dict keys: rom, title, output_dir, targets, jobs, force, regen,
ra, ra_popups, ra_toml, netplay.

GBA divergence from the sibling tools, verified against the framework
source: gbarecomp's runtime REQUIRES packages to include NO BIOS — the
launcher asks the player for their own GBA BIOS dump on first run and
remembers it (bios.cfg next to the game). The gba_bios_hle build only
layers BIOS SWI shortcuts over the real BIOS; it is not a BIOS-free
path (src/runtime/runtime.cpp resolve_asset hard-requires an image).
"""
import hashlib
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_VERSION = "1.0.1"

APP_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"
RA_BUNDLE = APP_DIR / "bundle" / "ra"
GBA_REPO_URL = "https://github.com/mstan/gbarecomp"
RECOMP_UI_REPO_URL = "https://github.com/RetroPortingToolKit/recomp-ui"
GAMEBOY_ADVANCE_CONSOLE_ID = 5  # RC_CONSOLE_GAMEBOY_ADVANCE (rc_consoles.h)

# ---------------------------------------------------------------------------
# Windows build support files
#
# The app OWNS its Windows toolchain support files (the GB tool learned
# this the hard way in the field on 2026-11-02: pointing at a
# hand-assembled ~/workspace/tools folder meant every Windows build
# died on the user's machine). They live in the app's own tools/
# folder, are validated file-by-file (never by folder existence), and
# are provisioned on first use. GBA_TOOLS_DIR can point at a complete
# prepared folder (e.g. the shared ~/workspace/tools on this VM, kept
# as a fallback when — and only when — it validates complete).
# ---------------------------------------------------------------------------

APP_TOOLS_DIR = APP_DIR / "tools"
LEGACY_TOOLS_DIR = Path.home() / "workspace" / "tools"

SDL2_MINGW_URL = ("https://github.com/libsdl-org/SDL/releases/download/"
                  "release-2.30.0/SDL2-devel-2.30.0-mingw.tar.gz")

# (filename inside tools/, human label) — the exact files the GBA
# Windows build validates before a build may start. The GBA runtime
# renders with desktop OpenGL (opengl32 is a Windows system library),
# so unlike the GB tool no Mesa EGL bundle is needed here.
_WIN_FILES = [
    ("SDL2-2.30.0/x86_64-w64-mingw32/lib/cmake/SDL2/sdl2-config.cmake",
     "SDL2 MinGW CMake config"),
    ("SDL2-2.30.0/x86_64-w64-mingw32/bin/SDL2.dll", "SDL2.dll"),
    ("SDL2-2.30.0/x86_64-w64-mingw32/include/SDL2/SDL.h", "SDL2 headers"),
]


def tools_dir():
    """The validated Windows-support folder: env override when set and
    complete, else the app's own tools/ (provisioned on demand), else
    the shared ~/workspace/tools when it validates complete."""
    env = os.environ.get("GBA_TOOLS_DIR")
    if env and _tools_complete(Path(env)):
        return Path(env)
    if _tools_complete(APP_TOOLS_DIR):
        return APP_TOOLS_DIR
    if _tools_complete(LEGACY_TOOLS_DIR):
        return LEGACY_TOOLS_DIR
    return APP_TOOLS_DIR


def _tools_complete(d):
    return all((d / rel).is_file() for rel, _ in _WIN_FILES)


def _download(url, dest, log=None):
    """Download url -> dest atomically (.part + rename). curl first —
    it rides this VM's proxy reliably — with a urllib fallback for
    machines where curl is absent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if shutil.which("curl"):
        r = subprocess.run(
            ["curl", "-fL", "--retry", "3", "--retry-delay", "2",
             "-o", str(part), url],
            capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and part.exists() and part.stat().st_size > 0:
            part.replace(dest)
            return
        if log:
            log_line(log, "curl failed, trying urllib: " +
                     (r.stderr or "").strip().splitlines()[-1]
                     if r.stderr else "curl error")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, \
            open(part, "wb") as f:
        shutil.copyfileobj(resp, f, 1024 * 256)
    part.replace(dest)


def ensure_win_tools(log=None):
    """Make sure the app's Windows support files are present and
    complete; download/build whatever is missing. Returns the folder."""
    d = tools_dir()
    if _tools_complete(d):
        return d
    d = APP_TOOLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    if not (d / _WIN_FILES[0][0]).is_file():
        log_line(log, "provisioning SDL2 MinGW development files "
                      "(one-time download)…")
        tgz = d / "SDL2-devel-2.30.0-mingw.tar.gz"
        _download(SDL2_MINGW_URL, tgz, log)
        with tarfile.open(tgz) as tf:
            tf.extractall(d)
        tgz.unlink(missing_ok=True)
    if not _tools_complete(d):
        missing = [label for rel, label in _WIN_FILES
                   if not (d / rel).is_file()]
        raise RuntimeError("Windows support files incomplete after "
                           "provisioning; missing: " + ", ".join(missing))
    log_line(log, f"Windows support files ready: {d}")
    return d


# Submodule directories of gbarecomp a game build actually needs (the
# Android SDL submodule is deliberately excluded).
GBA_SUBMODULES = ["external/arm-recomp-core", "external/rbengine",
                  "external/recomp-net"]


def log_line(log, msg):
    line = str(msg)
    if log:
        log.write(line + "\n")
        log.flush()
    else:
        print(line, flush=True)


def _run(cmd, log, cwd=None, timeout=3600, env=None):
    """Run a child, streaming its output to the log, with a REAL
    wall-clock timeout.

    Sibling of the GBRecomp Tools v1.0.1 fix (Duane, 2026-11-05):
    the old structure read the child's output stream to its end
    BEFORE starting the wait, so for a child that prints nothing
    and never exits, the stream never ended, the wait never
    started, and the TimeoutExpired kill branch below it was
    unreachable — a silent child hung the queue forever. The
    deadline is now enforced against the process itself: output
    is drained on a thread, and when the clock runs out the
    child's whole process group is killed and a RuntimeError
    names the full command and the timeout."""
    cmd = [str(c) for c in cmd]
    log_line(log, "+ " + " ".join(cmd))
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=run_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
        start_new_session=True)

    def _drain():
        try:
            for line in proc.stdout:
                log_line(log, line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        reader.join(timeout=5)
        raise RuntimeError(
            f"timed out after {timeout} s and was killed: {' '.join(cmd)}")
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc.returncode


# The GUI launches system python3 throughout the family; make sure the
# InterpretedPath it runs under can find tools installed outside PATH
# (clean-room shells in particular).
def _tool_env():
    env = dict(os.environ)
    extra = [str(APP_DIR / "tools"), str(LEGACY_TOOLS_DIR),
             str(Path.home() / ".cargo" / "bin")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _cmake_env():
    """Environment for CMake invocations: the tools folders must NOT
    be on PATH. CMake's find_package() searches prefixes derived
    from PATH entries, and a tools folder containing the SDL2 MinGW
    bundle (SDL2-2.30.0/cmake/sdl2-config.cmake one level down) then
    poisons LINUX builds into using the Windows SDL2 headers
    (imgui_impl_sdl2 dies on <windows.h>). Proven in isolation:
    same configure, tools-on-PATH -> SDL2_DIR=<tools>/SDL2-2.30.0/
    cmake; tools-off-PATH -> the system SDL2. This would hit any
    machine whose app tools/ folder is provisioned, not just this
    VM, so the strip is unconditional."""
    env = dict(os.environ)
    drop = {str(APP_TOOLS_DIR), str(LEGACY_TOOLS_DIR)}
    parts = [p for p in env.get("PATH", "").split(os.pathsep)
             if p and p not in drop]
    env["PATH"] = os.pathsep.join(parts)
    return env


def _ninja_args():
    """If ninja is not on the (stripped) PATH, point CMake at a
    portable copy explicitly."""
    if shutil.which("ninja"):
        return []
    for cand in (LEGACY_TOOLS_DIR / "ninja", APP_TOOLS_DIR / "ninja",
                 Path("/usr/local/bin/ninja")):
        if cand.is_file():
            return [f"-DCMAKE_MAKE_PROGRAM={cand}"]
    return []


# ---------------------------------------------------------------------------
# Toolbox check
# ---------------------------------------------------------------------------

def _cmd_ver(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0].strip() if first else "?"
    except Exception:
        return None


def _pkg_ver(pkg):
    try:
        out = subprocess.run(["pkg-config", "--modversion", pkg],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def toolbox():
    checks = []

    def add(name, version, pkg=None, apt=None, note=""):
        checks.append({"tool": name, "version": version,
                       "ok": bool(version), "apt": apt or pkg or "",
                       "note": note})

    add("CMake", _cmd_ver(["cmake", "--version"]), apt="cmake")
    add("Ninja", _cmd_ver(["ninja", "--version"])
        or _cmd_ver([str(LEGACY_TOOLS_DIR / "ninja"), "--version"]),
        apt="ninja-build")
    add("GCC (C)", _cmd_ver(["gcc", "-dumpfullversion"]), apt="gcc")
    add("G++ (C++)", _cmd_ver(["g++", "-dumpfullversion"]), apt="g++")
    add("Git", _cmd_ver(["git", "--version"]), apt="git")
    add("MinGW g++ (Windows builds)",
        _cmd_ver(["x86_64-w64-mingw32-g++", "-dumpfullversion"]),
        apt="g++-mingw-w64-x86-64")
    add("pkg-config", _cmd_ver(["pkg-config", "--version"]),
        apt="pkg-config")
    v = _pkg_ver("sdl2")
    add("SDL2 dev (pkg-config)", v, apt="libsdl2-dev",
        note="" if v else "needed by the Linux build's launcher")
    ssl = Path("/usr/include/openssl/ssl.h")
    add("OpenSSL dev (RA HTTPS)", "present" if ssl.exists() else None,
        apt="libssl-dev")
    png = Path("/usr/include/png.h")
    add("libpng dev (cover art)", "present" if png.exists() else None,
        apt="libpng-dev")
    xcb_cursor = _pkg_ver("xcb-cursor") or (
        "present" if Path("/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0")
        .exists() else None)
    add("libxcb-cursor (GUI display)", xcb_cursor,
        apt="libxcb-cursor0",
        note="" if xcb_cursor else
        "Qt needs it to open a window on X11 desktops")
    try:
        import PIL  # noqa: F401
        add("Pillow (Python)", "present")
    except Exception:
        add("Pillow (Python)", None, note="the launcher .sh installs it "
                                          "into the app venv on first run")
    add("curl (downloads)",
        "present" if shutil.which("curl") else None, apt="curl")
    td = tools_dir()
    for rel, label in _WIN_FILES:
        add(f"Win tools: {label}",
            "present" if (td / rel).is_file() else None,
            note="auto-downloaded on first Windows build"
            if not (td / rel).is_file() else "")
    return {"checks": checks,
            "all_ok": all(c["ok"] for c in checks
                          if not c["tool"].startswith("Win tools"))}


# ---------------------------------------------------------------------------
# ROM probe
#
# Mirrors the framework's own identification (src/gba/gba_rom_header.cpp
# parse_header / rom_signature, src/runtime/rom_loader.cpp
# detect_save_type):
#   title 0xA0..0xAB, game code 0xAC..0xAF, maker 0xB0..0xB1,
#   fixed byte 0xB2 == 0x96, version 0xBC, complement 0xBD where
#   sum = 0x19 + sum(rom[0xA0..0xBC]) and complement == (-sum) & 0xFF
#   (the framework's range starts at 0xA0 — the title — not 0xA2).
#   Logo sanity: of the 156 logo bytes, >140 zeros or >140 0xFF = bad.
#   Save signatures in framework priority order (longest first within
#   the FLASH family): EEPROM_V, SRAM_V, FLASH1M_V, FLASH512_V, FLASH_V.
#   Hard runtime limits: ROM must be <= 32 MiB (kRomMaxSize) and at
#   least one header long.
# ---------------------------------------------------------------------------

_SAVE_SIGNATURES = [
    ("EEPROM_V", "EEPROM"),
    ("SRAM_V", "SRAM"),
    ("FLASH1M_V", "Flash1M"),
    ("FLASH512_V", "Flash512"),
    ("FLASH_V", "Flash512"),
]

_REGION_HINTS = [
    ("(USA", "US"), ("(World", "World"), ("(Europe", "EU"),
    ("(Japan", "JP"), ("(Australia", "AU"), ("(Germany", "DE"),
    ("(France", "FR"), ("(Spain", "ES"), ("(Italy", "IT"),
    ("(Netherlands", "NL"), ("(Korea", "KR"), ("(Brazil", "BR"),
    ("(Canada", "CA"), ("(UK", "UK"), ("(Scandinavia", "SC"),
]


def _display_title(filename):
    name = Path(filename).stem
    # Strip trailing No-Intro tags: (USA) [..] etc. keep the bare title.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name)
    return name.strip() or Path(filename).stem


def _region_hint(filename):
    name = Path(filename).name
    for tag, code in _REGION_HINTS:
        if tag in name:
            return code
    return ""


def _detect_save(data):
    """Return (kind, signature) using the framework's priority order."""
    for prefix, kind in _SAVE_SIGNATURES:
        idx = data.find(prefix.encode())
        if idx >= 0:
            m = re.match(re.escape(prefix) + r"[0-9.]+",
                         data[idx:idx + 24].decode("ascii", "replace"))
            return kind, (m.group(0) if m else prefix)
    return "None", ""


def probe_rom(path):
    p = Path(path)
    data = p.read_bytes()
    size = len(data)
    result = {
        "path": str(p),
        "filename": p.name,
        "title": _display_title(p.name),
        "region": _region_hint(p.name),
        "size": size,
        "size_mb": round(size / (1024 * 1024), 2),
        "ok": False,
        "needs_review": False,
        "reasons": [],
    }
    crc = zlib.crc32(data) & 0xFFFFFFFF
    result["crc32"] = f"{crc:08x}"
    result["md5"] = hashlib.md5(data).hexdigest()
    result["sha1"] = hashlib.sha1(data).hexdigest()
    result["sha256"] = hashlib.sha256(data).hexdigest()
    if size < 0xC0:
        result["reasons"].append(
            "file is smaller than a GBA header (not a GBA ROM image)")
        return result
    hdr = data[:0xC0]
    result["header_title"] = hdr[0xA0:0xAC].decode("ascii", "replace") \
        .rstrip("\x00 ")
    result["game_code"] = hdr[0xAC:0xB0].decode("ascii", "replace")
    result["maker_code"] = hdr[0xB0:0xB2].decode("ascii", "replace")
    result["fixed_byte"] = hdr[0xB2]
    result["version"] = hdr[0xBC]
    result["complement"] = hdr[0xBD]
    expect = (-(0x19 + sum(data[0xA0:0xBD]))) & 0xFF
    result["complement_expected"] = expect
    result["complement_ok"] = (hdr[0xBD] == expect)
    logo = data[0x04:0x04 + 156]
    zeros = logo.count(0)
    ffs = logo.count(0xFF)
    result["logo_ok"] = not (zeros > 140 or ffs > 140)
    entry = struct.unpack_from("<I", data, 0)[0]
    result["entry_branch"] = (entry & 0xFF000000) == 0xEA000000
    kind, sig = _detect_save(data)
    result["save_type"] = kind
    result["save_signature"] = sig
    # needs-review: honest flags from the framework's real limits.
    reasons = result["reasons"]
    if size > 32 * 1024 * 1024:
        reasons.append(
            "ROM is larger than 32 MiB — gbarecomp's runtime rejects "
            "images over the GBA cartridge limit (kRomMaxSize); the "
            "64 MB Game Boy Advance Video carts use a special mapper")
    if not result["complement_ok"]:
        reasons.append(
            "header complement check fails (byte 0xBD) — damaged or "
            "trimmed dump; the runtime warns but the header is suspect")
    if result["fixed_byte"] != 0x96:
        reasons.append(
            "fixed header byte 0xB2 is not 0x96 — not a standard GBA "
            "header")
    if not result["logo_ok"]:
        reasons.append(
            "Nintendo logo area fails the framework's sanity check — "
            "likely a corrupt or non-GBA image")
    if not result["entry_branch"]:
        reasons.append(
            "entry point is not an ARM branch instruction — unusual "
            "image layout (homebrew/multiboot dumps look like this)")
    if "VIDEO" in result["header_title"].upper() or \
            "advance video" in p.name.lower():
        reasons.append(
            "Game Boy Advance Video title — video carts use a "
            "dedicated playback path the recomp runtime does not "
            "special-case")
    result["needs_review"] = bool(reasons)
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Cover art (libretro-thumbnails) — family policy: GitHub mirror FIRST
# (the canonical host rate-limits bursts), 3 attempts per host with
# backoff, No-Intro filename variants, response validation.
# ---------------------------------------------------------------------------

COVER_SYSTEM = "Nintendo_-_Game_Boy_Advance"
COVER_HOSTS = [
    "https://raw.githubusercontent.com/libretro-thumbnails/"
    "{sys}/master/Named_Boxarts",
    "https://thumbnails.libretro.com/{sys}/Named_Boxarts",
]
_ILLEGAL = dict.fromkeys(map(ord, '\\/:*?"<>|'), None)


def _lr_encode(name):
    return name.translate(_ILLEGAL).replace("&", "_")


def _cover_names(title):
    base = title.strip()
    names = [base]
    core = re.sub(r"\s*\([^)]*\)\s*$", "", base)
    core = re.sub(r"\s*\[[^\]]*\]\s*$", "", core).strip()
    if core and core != base:
        names.append(core)
    for cand in list(names):
        for suffix in (" (USA)", " (USA, Europe)", " (World)"):
            names.append(cand + suffix)
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _valid_cover(raw):
    return len(raw) >= 512 and raw[:8] == b"\x89PNG\r\n\x1a\n"


def fetch_cover(title, log=None, timeout=20):
    """Download box art PNG bytes for a title, or None. Cached in the
    app art-cache so repeat conversions don't re-download."""
    ART_CACHE.mkdir(parents=True, exist_ok=True)
    cache = ART_CACHE / (_lr_encode(title).replace(" ", "_") + ".png")
    if cache.exists() and _valid_cover(cache.read_bytes()):
        return cache.read_bytes()
    names = _cover_names(title)
    for template in COVER_HOSTS:
        base = template.format(sys=COVER_SYSTEM)
        for name in names:
            url = (f"{base}/"
                   + urllib.request.quote(_lr_encode(name) + ".png",
                                          safe=""))
            for attempt in range(3):
                try:
                    if shutil.which("curl"):
                        r = subprocess.run(
                            ["curl", "-fsL", "-o", str(cache) + ".part",
                             url], capture_output=True, timeout=timeout)
                        if r.returncode == 0:
                            raw = Path(str(cache) + ".part").read_bytes()
                            Path(str(cache) + ".part").unlink(
                                missing_ok=True)
                        else:
                            raw = b""
                    else:
                        req = urllib.request.Request(
                            url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req,
                                                    timeout=timeout) as resp:
                            raw = resp.read()
                    if _valid_cover(raw):
                        cache.write_bytes(raw)
                        log_line(log, f"cover art: {name} "
                                      f"({len(raw)} bytes)")
                        return raw
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
    log_line(log, f"cover art: no box art found for {title!r} "
                  "(game packages without it)")
    return None


def _png_to_tga(png_bytes, dest):
    """The recomp-ui launcher stages box art as an uncompressed TGA.
    Convert via Pillow (the app venv guarantees it)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        img.save(str(dest), format="TGA")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Framework resolution: env override -> app framework-cache -> dev
# checkout -> clone+build fallback. The app must be self-sufficient on
# a clean machine (clean-room rule).
# ---------------------------------------------------------------------------

def _gbarecomp_ok(d):
    return (d / "build" / "gba_recompile").is_file() and \
        (d / "tools" / "cli.py").exists()


def _recomp_ui_ok(d):
    return (d / "recomp_ui.cmake").exists()


def ensure_frameworks(log):
    gba_dir = None
    cands = []
    if os.environ.get("GBARECOMP_DIR"):
        cands.append(Path(os.environ["GBARECOMP_DIR"]))
    cands += [FRAMEWORK_CACHE / "gbarecomp",
              Path.home() / "workspace" / "gbarecomp"]
    for c in cands:
        if _gbarecomp_ok(c):
            gba_dir = c
            break
    if gba_dir is None:
        dest = FRAMEWORK_CACHE / "gbarecomp"
        FRAMEWORK_CACHE.mkdir(parents=True, exist_ok=True)
        if not (dest / "tools" / "cli.py").exists():
            log_line(log, f"cloning gbarecomp into {dest} …")
            if dest.exists():
                shutil.rmtree(dest)
            _run(["git", "clone", GBA_REPO_URL, str(dest)], log,
                 timeout=1800, env=_tool_env())
            _run(["git", "-C", str(dest), "submodule", "update", "--init"]
                 + GBA_SUBMODULES, log, timeout=1800, env=_tool_env())
        log_line(log, "building the gba_recompile analysis tool "
                      "(one-time framework build)…")
        _run(["cmake", "-S", str(dest), "-B", str(dest / "build"),
              "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"], log,
             timeout=1800, env=_tool_env())
        _run(["cmake", "--build", str(dest / "build"), "--target",
              "gba_recompile", "-j", "2"], log, timeout=3600,
             env=_tool_env())
        if not _gbarecomp_ok(dest):
            raise RuntimeError("gbarecomp clone/build did not produce "
                               "build/gba_recompile")
        gba_dir = dest
    log_line(log, f"gbarecomp: {gba_dir}")

    ui_dir = None
    ui_cands = []
    if os.environ.get("GBA_RECOMP_UI_DIR"):
        ui_cands.append(Path(os.environ["GBA_RECOMP_UI_DIR"]))
    ui_cands += [FRAMEWORK_CACHE / "recomp-ui",
                 Path.home() / "workspace" / "recomp-ui-dev",
                 Path.home() / "workspace" / "gb-recomp-tools-gui" /
                 "framework-cache" / "recomp-ui",
                 Path.home() / "workspace" / "snes-recomp-tools-gui" /
                 "framework-cache" / "recomp-ui"]
    for c in ui_cands:
        if _recomp_ui_ok(c):
            ui_dir = c
            break
    if ui_dir is None:
        dest = FRAMEWORK_CACHE / "recomp-ui"
        if not _recomp_ui_ok(dest):
            log_line(log, f"cloning recomp-ui into {dest} …")
            if dest.exists():
                shutil.rmtree(dest)
            _run(["git", "clone", RECOMP_UI_REPO_URL, str(dest)], log,
                 timeout=1800, env=_tool_env())
        if not _recomp_ui_ok(dest):
            raise RuntimeError("recomp-ui clone did not produce "
                               "recomp_ui.cmake")
        ui_dir = dest
    log_line(log, f"recomp-ui: {ui_dir}")
    return gba_dir, ui_dir


# ---------------------------------------------------------------------------
# Project scaffold: game.toml + the generic host (modelled on the
# framework's own reference game, SuperMarioAdvance4Recomp: game-root
# CMakeLists + src/main.cpp + an 8-line launcher boot wrapper; the
# game's extra dispatch hooks are game-specific and omitted — missed
# code falls through to the framework interpreter / self-heal).
# ---------------------------------------------------------------------------

def _camel(title):
    words = re.findall(r"[A-Za-z0-9]+", title)
    out = "".join(w[:1].upper() + w[1:] for w in words)
    return out or "Game"


def _c_string(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


GAME_TOML_TMPL = """# Generated by GBARecomp Tools from the ROM header and hashes.
# Regenerating overwrites this file; per-game data lives in the
# project's generated/ tree.
[game]
name = {name}
short_name = {short_name}

[program]
name = {program}
id = {game_code}
load_address = 0x08000000
size = {size:#x}
entry_pc = 0x08000000
codegen_shards = 64

[identity]
sha1 = {sha1}
md5 = {md5}

[rom]
sha1 = {sha1}

[runtime]
window_title = {name}

[video]
screen = "frontlit"
"""

MAIN_CPP_TMPL = """// Generic GBARecomp Tools host — no game-specific hooks. Code the
// static analysis missed (RAM-installed code, indirect dispatch) is
// covered by the framework's interpreter / self-healing tier.
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "runtime.h"
{extra_includes}
#if defined(GBAGAME_RECOMP_UI)
#include "game_launcher_boot.h"
#endif

int main(int argc, char** argv) {{
    for (int i = 1; i < argc; ++i) {{
        if (std::strcmp(argv[i], "--help") == 0 ||
            std::strcmp(argv[i], "-h") == 0) {{
            std::printf(
                "{camel} [--bios <path>] [--rom <path>] [game.toml]\\n"
                "\\n"
                "Windowed play opens the recomp-ui launcher first. "
                "--no-launcher skips it\\n"
                "for one run; --launcher forces it past the persisted "
                "skip-launcher setting.\\n"
                "Press Escape during play to open the in-game menu.\\n");
            return 0;
        }}
    }}

    gbarecomp::RunOptions opts;
    opts.builtin_game_name = "{name}";
    opts.builtin_rom_sha1 = "{sha1}";
    opts.launcher_game_config = "game.toml";
{extra_opts}
#if defined(GBAGAME_RECOMP_UI)
    std::vector<std::string> args(argv, argv + argc);
    if (game_launcher_preboot(args, opts)) return 0;
    std::vector<char*> av;
    av.reserve(args.size());
    for (auto& arg : args) av.push_back(arg.data());
    return gbarecomp::run_game(static_cast<int>(av.size()),
                               av.data(), opts);
#else
    return gbarecomp::run_game(argc, argv, opts);
#endif
}}
"""

GAME_LAUNCHER_BOOT_H = """#pragma once

#include <string>
#include <vector>

#include "runtime.h"

int game_launcher_preboot(std::vector<std::string>& args,
                          const gbarecomp::RunOptions& opts);
"""

GAME_LAUNCHER_BOOT_CPP = """#include "launcher_seam.h"

#include "game_launcher_boot.h"

int game_launcher_preboot(std::vector<std::string>& args,
                          const gbarecomp::RunOptions& opts) {
    return gbarecomp_launcher_preboot(args, opts);
}
"""

CMAKELISTS_TMPL = """cmake_minimum_required(VERSION 3.20)
project({camel} C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(GBARECOMP_ROOT "{gba_root}"
    CACHE PATH "Path to the gbarecomp framework checkout")
set(RECOMP_UI_ROOT "{ui_root}"
    CACHE PATH "Path to the shared recomp-ui checkout")
set(GBARECOMP_RUNTIME_UI_ROOT "${{RECOMP_UI_ROOT}}"
    CACHE PATH "recomp-ui root used by the live GBA runtime adapter")
if(NOT EXISTS "${{GBARECOMP_ROOT}}/CMakeLists.txt")
    message(FATAL_ERROR "GBARECOMP_ROOT is not a gbarecomp checkout: ${{GBARECOMP_ROOT}}")
endif()

add_subdirectory("${{GBARECOMP_ROOT}}" gbarecomp_build EXCLUDE_FROM_ALL)
file(GLOB GAME_GENERATED_SOURCES CONFIGURE_DEPENDS
    "${{CMAKE_CURRENT_SOURCE_DIR}}/generated/*.c"
    "${{CMAKE_CURRENT_SOURCE_DIR}}/generated/*.cpp")

add_executable({camel}
    src/main.cpp
    ${{GAME_GENERATED_SOURCES}})

set(GAME_RECOMPILED_SOURCES ${{GAME_GENERATED_SOURCES}})
list(FILTER GAME_RECOMPILED_SOURCES INCLUDE REGEX
    "/recompiled_[0-9]+\\\\.cpp$")
if(GAME_RECOMPILED_SOURCES)
    set_source_files_properties(${{GAME_RECOMPILED_SOURCES}} PROPERTIES
        COMPILE_OPTIONS "-O1;-g0")
endif()

target_include_directories({camel} PRIVATE
    "${{CMAKE_CURRENT_SOURCE_DIR}}/generated"
    "${{CMAKE_CURRENT_SOURCE_DIR}}/src"
    "${{GBARECOMP_ROOT}}/src/armv4t"
    "${{GBARECOMP_ROOT}}/src/gba"
    "${{GBARECOMP_ROOT}}/src/runtime"
    "${{GBARECOMP_ROOT}}/src/debug")

target_link_libraries({camel} PRIVATE
    "-Wl,--start-group"
    gbarecomp_runtime
    gbarecomp_debug
    gbarecomp_gba
    gbarecomp_armv4t
    "-Wl,--end-group")

target_compile_definitions({camel} PRIVATE
    GBARECOMP_DEFAULT_DEBUG_PORT=19883
    GBARECOMP_WINDOW_TITLE="{camel}"
    GBARECOMP_DEFAULT_GAME_CONFIG="game.toml")

option(GBAGAME_RECOMP_UI "Build the recomp-ui launcher and in-game menu" ON)
if(GBAGAME_RECOMP_UI)
    if(NOT EXISTS "${{RECOMP_UI_ROOT}}/recomp_ui.cmake")
        message(FATAL_ERROR "recomp-ui is not available at ${{RECOMP_UI_ROOT}}")
    endif()
    include("${{RECOMP_UI_ROOT}}/recomp_ui.cmake")
    add_library(game_launcher_ui STATIC src/game_launcher_boot.cpp)
    target_include_directories(game_launcher_ui PRIVATE
        "${{GBARECOMP_ROOT}}/src/runtime"
        "${{GBARECOMP_ROOT}}/src/gba"
        src)
    recomp_target_launcher_ui(game_launcher_ui
        CONSOLE gba{boxart_arg})
    find_package(SDL2 REQUIRED)
    target_link_libraries(game_launcher_ui PUBLIC SDL2::SDL2)
    target_link_libraries({camel} PRIVATE game_launcher_ui)
    set_property(SOURCE src/main.cpp APPEND PROPERTY
        COMPILE_DEFINITIONS GBAGAME_RECOMP_UI)
endif()

if(MSVC)
    target_link_options({camel} PRIVATE /STACK:16777216)
elseif(MINGW OR WIN32)
    target_link_options({camel} PRIVATE -Wl,--stack,16777216)
endif()
{netplay_block}{ra_block}"""

NETPLAY_CMAKE_BLOCK = """
# Link play (framework adapter, GBARECOMP_NETPLAY=ON at configure):
# The framework glue also creates its own launcher-seam TEST
# executable that links the game target — a framework dev test, not
# part of the shipped game, and an executable target cannot be
# linked that way from a standalone game project. Pre-declaring the
# (empty) target keeps the glue's NOT-TARGET guard from creating it.
if(NOT TARGET multiplayer_launcher_seam_tests)
    add_custom_target(multiplayer_launcher_seam_tests)
endif()
if(TARGET gbarecomp_netplay)
    gbarecomp_target_netplay_launcher({camel})
    # The launcher-seam TU (game_launcher_boot.cpp) compiles in the
    # game_launcher_ui library, but gbarecomp_target_netplay_launcher
    # puts RECOMP_UI_HAS_NETPLAY_HOST and the backend link on the
    # game target only. Without them on the library too,
    # configure_netplay compiles out — the build links the whole
    # netplay adapter yet the launcher never offers a NETPLAY
    # button — and the seam's backend calls would not resolve.
    target_compile_definitions(game_launcher_ui PRIVATE
        RECOMP_UI_HAS_NETPLAY_HOST=1)
    if(TARGET recomp_launcher_netplay)
        target_link_libraries(game_launcher_ui PUBLIC
            recomp_launcher_netplay)
    endif()
endif()
"""

RA_CMAKE_BLOCK = """
# RetroAchievements (GBARecomp Tools bundle/ra) — managed region.
if(EXISTS "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_extras.cpp")
    file(GLOB_RECURSE GBA_RA_RCHEEVOS_SOURCES
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra-rcheevos/src/*.c")
    list(FILTER GBA_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")
    list(FILTER GBA_RA_RCHEEVOS_SOURCES EXCLUDE REGEX
        "rc_client_raintegration\\\\.c$")
    target_sources({camel} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_extras.cpp"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_http.c"
        ${{GBA_RA_RCHEEVOS_SOURCES}})
    target_include_directories({camel} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra-rcheevos/include"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra"
        "${{GBARECOMP_ROOT}}/src")
    if(WIN32)
        target_link_libraries({camel} PRIVATE winhttp)
    else()
        find_package(OpenSSL REQUIRED)
        target_link_libraries({camel} PRIVATE OpenSSL::SSL OpenSSL::Crypto)
    endif()
{popups_def}endif()
"""

TOOLCHAIN_MINGW = """set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""


def _ra_bundle_ok():
    return ((RA_BUNDLE / "ra_extras.cpp").exists()
            and (RA_BUNDLE / "ra_http.c").exists()
            and (RA_BUNDLE / "ra-rcheevos" / "include" /
               "rc_client.h").exists())


def scaffold_project(job, probe, gba_dir, ui_dir, log,
                     ra_on=False, netplay_on=True):
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    title = job.get("title") or probe["title"]
    camel = _camel(title)
    proj = out_dir / (camel + "-src")
    (proj / "src").mkdir(parents=True, exist_ok=True)
    staged_rom = proj / (camel + ".gba")
    shutil.copy2(job["rom"], staged_rom)

    (proj / "game.toml").write_text(GAME_TOML_TMPL.format(
        name=_toml_string(title), short_name=_toml_string(camel),
        program=_toml_string(camel),
        game_code=_toml_string(probe["game_code"]),
        size=probe["size"], sha1=_toml_string(probe["sha1"]),
        md5=_toml_string(probe["md5"])))
    log_line(log, f"game.toml: {title} [{probe['game_code']}] "
                  f"save={probe['save_type']} "
                  f"sha1={probe['sha1'][:12]}…")

    gen = proj / "generated"
    if job.get("regen") and gen.exists():
        shutil.rmtree(gen)
    if not (gen / "dispatch_table.cpp").exists():
        log_line(log, "generating recompiled sources with "
                      "gba_recompile (this can take a while)…")
        _run([str(gba_dir / "build" / "gba_recompile"),
              "--rom", str(staged_rom),
              "--config", str(proj / "game.toml"),
              "--out", str(gen),
              "--max-functions", "65536"],
             log, cwd=proj, timeout=3600, env=_tool_env())
    if not (gen / "dispatch_table.cpp").exists():
        raise RuntimeError("gba_recompile produced no dispatch table")

    extra_includes = ""
    extra_opts = ""
    if ra_on:
        extra_includes += '#include "ra_extras.h"\n'
        extra_opts += "    opts.host_overlay = &gba_ra_overlay;\n"
    if netplay_on:
        extra_includes += '#include "multiplayer_launch.h"\n'
        extra_opts += (
            "    opts.netplay = gbarecomp::make_gba_netplay_launch(\n"
            f'        "{_c_string(camel)}", '
            f'"{probe["sha1"]}-gbarecomp-tools",\n'
            f'        "{probe["sha256"]}");\n')
    (proj / "src" / "main.cpp").write_text(MAIN_CPP_TMPL.format(
        name=_c_string(title), sha1=probe["sha1"], camel=camel,
        extra_includes=extra_includes, extra_opts=extra_opts))
    (proj / "src" / "game_launcher_boot.h").write_text(
        GAME_LAUNCHER_BOOT_H)
    (proj / "src" / "game_launcher_boot.cpp").write_text(
        GAME_LAUNCHER_BOOT_CPP)

    # RetroAchievements bundle: copied fresh on every scaffold so an
    # updated app updates existing projects.
    ra_dir = proj / "ra"
    if ra_on:
        if not _ra_bundle_ok():
            raise RuntimeError("RA requested but bundle/ra is "
                               "incomplete next to the app")
        if ra_dir.exists():
            shutil.rmtree(ra_dir)
        shutil.copytree(RA_BUNDLE, ra_dir)
        log_line(log, "RetroAchievements module copied into project")
    elif ra_dir.exists():
        shutil.rmtree(ra_dir)

    # Box art for the launcher (staged into assets/img at build time).
    cover = fetch_cover(title, log=log)
    boxart_arg = ""
    if cover and _png_to_tga(cover, proj / "boxart.tga"):
        boxart_arg = ("\n    BOXART "
                      "${CMAKE_CURRENT_SOURCE_DIR}/boxart.tga")
        log_line(log, "box art staged for the launcher")
    ra_block = ""
    if ra_on:
        popups_def = "" if job.get("ra_popups", True) else \
            f"    target_compile_definitions({camel} PRIVATE " \
            "RA_DISABLE_POPUPS=1)\n"
        ra_block = RA_CMAKE_BLOCK.format(camel=camel,
                                         popups_def=popups_def)
    (proj / "CMakeLists.txt").write_text(CMAKELISTS_TMPL.format(
        camel=camel,
        gba_root=str(gba_dir).replace("\\", "/"),
        ui_root=str(ui_dir).replace("\\", "/"),
        boxart_arg=boxart_arg,
        netplay_block=NETPLAY_CMAKE_BLOCK.format(camel=camel)
        if netplay_on else "",
        ra_block=ra_block))
    (proj / "tc-mingw.cmake").write_text(TOOLCHAIN_MINGW)
    return proj, camel, title, cover


# ---------------------------------------------------------------------------
# Build / verify
# ---------------------------------------------------------------------------

def build_target(proj, camel, target, jobs, gba_dir, ui_dir, log,
                 netplay_on=True):
    common = ["-DGBARECOMP_ROOT=" + str(gba_dir).replace("\\", "/"),
              "-DRECOMP_UI_ROOT=" + str(ui_dir).replace("\\", "/")]
    if netplay_on:
        common.append("-DGBARECOMP_NETPLAY=ON")
    if target == "linux":
        bdir = proj / "build-linux"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release"] + common + _ninja_args()
    else:
        bdir = proj / "build-win"
        tools = ensure_win_tools(log)
        sdl2_prefix = tools / "SDL2-2.30.0" / "x86_64-w64-mingw32"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_TOOLCHAIN_FILE=" + str(proj / "tc-mingw.cmake"),
               "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_PREFIX_PATH=" + str(sdl2_prefix)] + common \
            + _ninja_args()
    _run(cmd, log, timeout=1800, env=_cmake_env())
    # Build only the game target: a netplay configure also defines
    # the framework's own test/peer executables, which a shipped
    # conversion does not need to compile.
    _run(["cmake", "--build", str(bdir), "--target", camel,
          "-j", str(jobs)], log, timeout=7200, env=_cmake_env())
    exe = bdir / (camel + (".exe" if target == "windows" else ""))
    return bdir, exe


def verify_build(bdir, exe, proj):
    if not exe.exists() or exe.stat().st_size < 1_000_000:
        return {"ok": False,
                "reason": f"binary missing or too small: {exe}"}
    if not (proj / "generated" / "dispatch_table.cpp").exists():
        return {"ok": False, "reason": "generated sources not found"}
    if not (bdir / "assets" / "img").is_dir():
        return {"ok": False,
                "reason": "launcher assets not staged in build dir"}
    return {"ok": True, "size": exe.stat().st_size}


def _mingw_dll(name):
    out = subprocess.run(
        ["x86_64-w64-mingw32-g++", "-print-file-name=" + name],
        capture_output=True, text=True, timeout=15)
    p = Path(out.stdout.strip())
    return p if p.exists() else None


def _win_dlls():
    t = tools_dir()
    out = [("SDL2.dll", t / "SDL2-2.30.0" / "x86_64-w64-mingw32" /
            "bin" / "SDL2.dll")]
    # MinGW runtimes, stripped (an unstripped libstdc++-6.dll is huge).
    strip = shutil.which("x86_64-w64-mingw32-strip")
    cache = APP_TOOLS_DIR / "dll-cache"
    for name in ("libstdc++-6.dll", "libgcc_s_seh-1.dll",
                 "libwinpthread-1.dll"):
        src = _mingw_dll(name)
        if src is None:
            continue
        if strip:
            cache.mkdir(parents=True, exist_ok=True)
            dst = cache / name
            if not dst.exists() or \
                    dst.stat().st_mtime < src.stat().st_mtime:
                shutil.copy2(src, dst)
                subprocess.run([strip, str(dst)], capture_output=True,
                               timeout=60)
            out.append((name, dst))
        else:
            out.append((name, src))
    return out


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

README_TMPL = """{title} - native PC recompilation (Game Boy Advance)
=========================================================

Built with GBARecomp Tools on the gbarecomp static-recompilation
framework by Matthew Stanley (mstan) / RetroPortingToolkit
(retroportingtoolkit.com - github.com/mstan,
github.com/RetroPortingToolKit). Framework license: PolyForm
Noncommercial 1.0.0. Launcher (recomp-ui): MIT, (c) 2026
Matthew Stanley.

This package contains NO game ROM and NO BIOS. It runs the ROM you
converted it from - keep your own dump; the game verifies the ROM's
SHA-1 ({sha1}) at startup.

HOW TO PLAY
  Run {exe} and the launcher opens: it remembers your ROM and BIOS
  choices after the first run (rom.cfg / bios.cfg next to the game).

BIOS - YOU NEED YOUR OWN DUMP (one time)
  The Game Boy Advance firmware is copyrighted, so no package can
  include it. On first launch the game asks you to locate a GBA BIOS
  dump (usually named gba_bios.bin, exactly 16 KiB - a dump from your
  own hardware). The choice is remembered; you only do this once per
  package. The recompiled runtime cannot run without it. The
  launcher checks the dump's SHA-1: use a genuine retail dump (the
  standard gba_bios.bin) — replacement/open BIOS images are reported
  as "BIOS not ready" by the launcher's Play and lobby gates even
  though the runtime itself can boot them from the command line
  (--bios). The launcher also offers an OpenBIOS mode in that
  dialog.

YOUR SAVE
  Battery/EEPROM/Flash saves ({save}) are kept as a .sav file with
  the game and reload automatically.

LINK PLAY (NETPLAY)
  Two players, link-cable style, from the launcher's NETPLAY screen.
  Both sides must run the SAME build with the SAME verified ROM.
  LAN play and direct-IP play are supported. The full lobby flow
  (mode picker, LAN lobby list, host dialog, seating, chat, match
  settings) is verified working in the shipped build. A connected
  match could not be demonstrated from the converter's build
  sandbox: its firewall blocks the session traffic (UDP), and its
  only BIOS is an open-source replacement whose SHA-1 the
  launcher's bootability gate rejects for lobby play (it expects
  the retail dump, which you have). Please test a real match on
  your own machines.
{ra_block}
Game code: {game_code}   ROM size: {size_mb} MB   Save type: {save}
"""

RA_BLOCK_ON = """
RETROACHIEVEMENTS
  retroachievements.toml sits next to the game with your username and
  Connect API token; achievements unlock in-game with pop-up
  notifications and a login toast at start. NOTE: RetroAchievements
  counts hardcore only from approved clients, and recompiled clients
  are not approved clients yet - unlocks register as Casual/softcore
  even with hardcore = true in the toml. (This is an RA policy gate,
  not a bug; never spoof another client's identity to bypass it.)
"""

RA_BLOCK_OFF = """
RETROACHIEVEMENTS
  This build was made with RetroAchievements off.
"""


def package(job, probe, proj, camel, title, cover, bdir, exe, target,
            log):
    stage_root = Path(job["output_dir"]) / ".packages"
    pkg_name = f"{title} (GBA)"
    pkg_dir = stage_root / pkg_name / target
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)
    exe_dest = pkg_dir / exe.name
    shutil.copy2(exe, exe_dest)
    if target != "windows":
        exe_dest.chmod(0o755)
    assets = bdir / "assets"
    if assets.is_dir():
        shutil.copytree(assets, pkg_dir / "assets")
    shutil.copy2(proj / "game.toml", pkg_dir / "game.toml")
    if target == "windows":
        for name, src in _win_dlls():
            if src.exists():
                shutil.copy2(src, pkg_dir / name)
            else:
                log_line(log, f"WARNING: Windows runtime DLL missing: "
                              f"{name}")
    ra_on = bool(job.get("ra", True))
    if ra_on:
        toml_src = job.get("ra_toml") or str(
            Path.home() / "workspace" / "retroachievements-hardcore.toml")
        if toml_src and Path(toml_src).exists():
            shutil.copy2(toml_src, pkg_dir / "retroachievements.toml")
        else:
            log_line(log, "WARNING: no filled retroachievements.toml "
                          "found; shipping the blank example only")
        example = RA_BUNDLE / "retroachievements.toml.example"
        if example.exists():
            shutil.copy2(example,
                         pkg_dir / "retroachievements.toml.example")
    if cover:
        art_dir = pkg_dir / "artwork" / title
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "front.png").write_bytes(cover)
    readme = README_TMPL.format(
        title=title, exe=exe.name, sha1=probe["sha1"],
        save=probe["save_type"], short=camel,
        ra_block=(RA_BLOCK_ON if ra_on else RA_BLOCK_OFF),
        game_code=probe["game_code"], size_mb=probe["size_mb"])
    (pkg_dir / "README.txt").write_text(readme)
    zip_path = Path(job["output_dir"]) / f"{pkg_name} - {target}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full in sorted(pkg_dir.rglob("*")):
            if full.is_file():
                zf.write(full, full.relative_to(pkg_dir.parent.parent))
    log_line(log, f"package: {zip_path} ({zip_path.stat().st_size} "
                  "bytes)")
    return zip_path


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job, log):
    rom = Path(job["rom"])
    if not rom.exists():
        raise RuntimeError(f"ROM not found: {rom}")
    probe = probe_rom(rom)
    if not probe["ok"]:
        raise RuntimeError("probe failed: " +
                           "; ".join(probe["reasons"]))
    if probe["needs_review"] and not job.get("force"):
        log_line(log, "NOTE: probe flags this ROM for review: " +
                      "; ".join(probe["reasons"]))
    ra_on = bool(job.get("ra", True))
    if ra_on and not probe["sha1"]:
        ra_on = False
    netplay_on = bool(job.get("netplay", True))
    gba_dir, ui_dir = ensure_frameworks(log)
    proj, camel, title, cover = scaffold_project(
        job, probe, gba_dir, ui_dir, log, ra_on=ra_on,
        netplay_on=netplay_on)
    jobs = int(job.get("jobs", 2))
    packages = []
    failures = {}
    failed_targets = []
    built = {}
    for target in job.get("targets", ["linux", "windows"]):
        try:
            bdir, exe = build_target(proj, camel, target, jobs,
                                     gba_dir, ui_dir, log,
                                     netplay_on=netplay_on)
            v = verify_build(bdir, exe, proj)
            if not v["ok"]:
                raise RuntimeError("verify failed: " + v["reason"])
            log_line(log, f"verify {target}: ok ({v['size']} bytes)")
            packages.append(str(package(job, probe, proj, camel, title,
                                        cover, bdir, exe, target, log)))
            built[target] = v["size"]
        except Exception as e:  # per-target isolation: one target's
            failures[target] = str(e)  # failure must not sink the job
            failed_targets.append(target)
            log_line(log, f"ERROR [{target}]: {e}")
    if not packages:
        raise RuntimeError("all targets failed: " +
                           json.dumps(failures))
    return {"title": title, "probe": probe, "packages": packages,
            "built": built, "failures": failures,
            "failed_targets": failed_targets,
            "project": str(proj),
            "status": "done" if not failures else "partial"}


def main(argv):
    if len(argv) >= 3 and argv[1] == "probe":
        print(json.dumps(probe_rom(argv[2]), indent=1))
        return 0
    if len(argv) >= 2 and argv[1] == "toolbox":
        print(json.dumps(toolbox(), indent=1))
        return 0
    if len(argv) >= 4 and argv[1] == "run" and argv[2] == "--job":
        job = json.loads(Path(argv[3]).read_text())
        result = run_job(job, sys.stdout)
        print(json.dumps(result, indent=1))
        return 0
    print("usage: engine.py probe <rom> | toolbox | run --job <json>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
