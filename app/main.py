#!/usr/bin/env python3
"""GBARecomp Tools — convert Game Boy Advance ROMs to native PC
games.

Pick ROMs -> they are probed instantly from their cartridge headers
-> Run Queue converts them one at a time (gbarecomp: recomp-ui
launcher, box art, RetroAchievements with in-game pop-ups, link
play) and packages Linux + Windows zips. Failures are logged and
skipped, never fatal to the queue. ROMs the framework may not handle
cleanly are flagged "needs review" instead of being guessed at.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_TOML = Path.home() / "workspace" / "retroachievements-hardcore.toml"

STATUS_COL = 5

HELP_TEXT = """\
GBARecomp Tools — turn your own Game Boy Advance cartridges' ROM
dumps into native PC games: no emulator, the game's code is
recompiled to a real executable with a launcher, box art,
RetroAchievements and link-cable play.

HOW IT WORKS
  Add .gba files (or a whole folder). Each ROM is probed instantly
  from its cartridge header: title, game code, save type (EEPROM /
  SRAM / Flash), ROM size, region. Press Run Queue and each game is
  converted one at a time and packaged as
  "<Game> (GBA) - linux.zip" / "<Game> (GBA) - windows.zip" in the
  output folder. A game that fails is marked and the queue moves
  on; if one target fails, the other still packages and the row
  says which one you got.

THE GAMES THEMSELVES — SUPPLY YOUR OWN ROM
  The package contains the executable, its launcher assets and a
  README. It does NOT contain the ROM: the launcher asks for your
  own dump of the exact cartridge the first time and checks its
  SHA-1 fingerprint before it will play. Keep the assets/ folder
  next to the executable — the launcher's art, fonts and icons
  live there.

BIOS — YOU NEED YOUR OWN DUMP (one time per package)
  Unlike the Game Boy tools, the Game Boy Advance firmware is
  required by these builds and cannot be included: on first
  launch the game asks you to locate a GBA BIOS dump (usually
  gba_bios.bin, exactly 16 KiB, dumped from your own hardware).
  The choice is remembered in bios.cfg next to the game, so you
  only do it once per package. There is no BIOS-free mode: the
  runtime's HLE shortcuts layer over the real BIOS, they do not
  replace it. The launcher checks the dump's SHA-1 and expects
  the genuine retail dump — replacement/open BIOS images are
  reported "BIOS not ready" by its Play and lobby gates (the
  runtime itself can still boot them via --bios on the command
  line). The launcher also offers an OpenBIOS mode.

COVER ART
  Box art is fetched automatically from the libretro-thumbnails
  project (Nintendo - Game Boy Advance set) and baked into the
  launcher, and is also included in each package under artwork/.
  A few games have no art under their exact name; those launchers
  just show the placeholder art.

RETROACHIEVEMENTS
  With RA on (default), each game includes RetroAchievements: the
  converter copies your filled retroachievements.toml into every
  package (Settings has the picker; it defaults to your saved
  toml). You'll see a "Logged in" toast in game, and achievement
  unlocks pop up the same way. The toml holds your username and
  your api_token — use the token from retroachievements.org under
  Settings (the Connect token), in double quotes; hardcore
  unquoted. The Settings pop-ups checkbox hides both the toast
  and the pop-ups. Note that RetroAchievements counts hardcore
  only from approved clients; recomp clients currently earn
  Casual/softcore unlocks.

LINK PLAY (link cable over the network)
  Link-cable games can be played between machines running the
  SAME build with the SAME verified ROM, from the launcher's
  NETPLAY screen — LAN lobbies and direct-IP sessions, with
  seating, chat and match settings (all verified working in the
  shipped build). A full connected match has not yet been
  demonstrated from the converter's build environment: its
  sandbox blocks the session traffic, and its only BIOS is an
  open-source replacement the launcher's bootability gate
  rejects for lobby play (it checks the BIOS SHA-1 and expects
  the retail dump). Verify a real match on your own machines.

"NEEDS REVIEW"
  The probe flags ROMs the framework may not handle cleanly:
  images over the 32 MB cartridge limit (the 64 MB Game Boy
  Advance Video carts), headers that fail the complement/logo
  checks, non-standard entry points, and GBA Video titles.
  Flagged games still convert if you run them — the flag means
  "check this one by hand", and a passing probe is not a
  guarantee any given game converts perfectly.

CREDITS
  Built on gbarecomp by Matthew Stanley (mstan), part of the
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit).
  gbarecomp license: PolyForm Noncommercial 1.0.0.
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
"""


class Worker(QThread):
    log = Signal(str)
    status = Signal(int, str)
    finished_all = Signal()

    def __init__(self, items, settings):
        super().__init__()
        self.items = items          # list of (row, job-dict)
        self.settings = settings

    def run(self):
        for row, job in self.items:
            self.status.emit(row, "converting…")
            try:
                result = engine.run_job(job, log=self.log.emit)
                if result.get("failed_targets"):
                    ok = ", ".join(sorted(result.get("built", {})))
                    self.status.emit(row, f"done ({ok} only)")
                    self.log.emit(
                        f"PARTIAL {job.get('title')}: packaged {ok}; "
                        "failed: " + "; ".join(
                            f"{t}: {m}" for t, m in
                            result["failures"].items()))
                else:
                    self.status.emit(row, "done")
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"FAILED {job.get('title')}: {e}")
                self.status.emit(row, "failed")
        self.finished_all.emit()


class CoverWorker(QThread):
    done = Signal(str, object)

    def __init__(self, title):
        super().__init__()
        self.title = title

    def run(self):
        try:
            p = engine.fetch_cover(self.title)
        except Exception:  # noqa: BLE001
            p = None
        self.done.emit(self.title, p)


class InstallWorker(QThread):
    done = Signal(bool)

    def __init__(self, packages):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            subprocess.run(["pkexec", "apt-get", "update"], check=False)
            r = subprocess.run(["pkexec", "apt-get", "install", "-y"]
                               + self.packages, check=False)
            self.done.emit(r.returncode == 0)
        except Exception:  # noqa: BLE001
            self.done.emit(False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GBARecomp Tools")
        self.resize(1180, 760)
        self.items = []             # list of probe dicts
        self.worker = None
        self.cover_worker = None
        self._build_ui()
        self._load_settings()
        self.refresh_toolbox()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        tabs = QTabWidget()
        tabs.addTab(self._convert_tab(), "Convert")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")
        self.setCentralWidget(tabs)

    def _convert_tab(self):
        root = QWidget()
        v = QVBoxLayout(root)
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Code", "Save", "ROM", "Region", "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_selected)
        lv.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn in [("Add ROMs…", self.add_roms),
                          ("Add Folder…", self.add_folder),
                          ("Remove", self.remove_selected),
                          ("Clear", self.clear_queue)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._probe_box())
        rv.addWidget(self._settings_box())
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([640, 480])
        v.addWidget(split, 3)

        run_row = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: …")
        run_row.addWidget(self.toolbox_label)
        self.install_btn = QPushButton("Install missing packages")
        self.install_btn.clicked.connect(self.install_packages)
        run_row.addWidget(self.install_btn)
        run_row.addStretch(1)
        self.run_btn = QPushButton("Run Queue")
        self.run_btn.clicked.connect(self.run_queue)
        run_row.addWidget(self.run_btn)
        v.addLayout(run_row)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        v.addWidget(self.log_edit, 2)
        root._split = split
        return root

    def _probe_box(self):
        box = QGroupBox("ROM details")
        form = QFormLayout(box)
        self.p_title = QLabel("—")
        self.p_code = QLabel("—")
        self.p_save = QLabel("—")
        self.p_sizes = QLabel("—")
        self.p_region = QLabel("—")
        self.p_crc = QLabel("—")
        self.p_cover = QLabel("")
        self.p_cover.setAlignment(Qt.AlignCenter)
        form.addRow("Header title:", self.p_title)
        form.addRow("Game code:", self.p_code)
        form.addRow("Save type:", self.p_save)
        form.addRow("ROM size:", self.p_sizes)
        form.addRow("Region:", self.p_region)
        form.addRow("CRC32 / SHA-1:", self.p_crc)
        form.addRow(self.p_cover)
        return box

    def _settings_box(self):
        box = QGroupBox("Settings (apply to the whole queue)")
        form = QFormLayout(box)
        self.s_ra = QCheckBox("RetroAchievements")
        self.s_ra.setChecked(True)
        self.s_popups = QCheckBox("Achievement pop-ups + login toast")
        self.s_popups.setChecked(True)
        self.s_netplay = QCheckBox("Link play (netplay)")
        self.s_netplay.setChecked(True)
        form.addRow(self.s_ra)
        form.addRow(self.s_popups)
        form.addRow(self.s_netplay)
        toml_row = QHBoxLayout()
        self.s_toml = QLineEdit()
        if DEFAULT_TOML.exists():
            self.s_toml.setText(str(DEFAULT_TOML))
        pick = QPushButton("Pick…")
        pick.clicked.connect(lambda: self._pick_file(self.s_toml,
                                                     "TOML (*.toml)"))
        toml_row.addWidget(self.s_toml)
        toml_row.addWidget(pick)
        form.addRow("RA toml to include:", toml_row)
        tgt_row = QHBoxLayout()
        self.s_linux = QCheckBox("Linux")
        self.s_linux.setChecked(True)
        self.s_windows = QCheckBox("Windows")
        self.s_windows.setChecked(True)
        tgt_row.addWidget(self.s_linux)
        tgt_row.addWidget(self.s_windows)
        form.addRow("Build targets:", tgt_row)
        self.s_jobs = QSpinBox()
        self.s_jobs.setRange(1, 16)
        self.s_jobs.setValue(2)
        form.addRow("Build jobs:", self.s_jobs)
        out_row = QHBoxLayout()
        self.s_out = QLineEdit(str(APP_DIR / "output"))
        pick_o = QPushButton("Pick…")
        pick_o.clicked.connect(lambda: self._pick_dir(self.s_out))
        out_row.addWidget(self.s_out)
        out_row.addWidget(pick_o)
        form.addRow("Output folder:", out_row)
        return box

    # ------------------------------------------------------------ util
    def _pick_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Pick a file", "", filt)
        if p:
            edit.setText(p)

    def _pick_dir(self, edit):
        p = QFileDialog.getExistingDirectory(self, "Pick a folder")
        if p:
            edit.setText(p)

    def log_line(self, msg):
        self.log_edit.appendPlainText(str(msg))

    def refresh_toolbox(self):
        try:
            t = engine.toolbox()
            missing = sorted({c["apt"] for c in t["checks"]
                              if not c["ok"] and c["apt"]})
            self._missing = missing
            if t["all_ok"]:
                self.toolbox_label.setText("Toolbox: ready")
                self.install_btn.setEnabled(False)
            else:
                names = [c["tool"] for c in t["checks"] if not c["ok"]]
                self.toolbox_label.setText(
                    "Toolbox: missing " + ", ".join(names))
                self.install_btn.setEnabled(bool(missing))
        except Exception as e:  # noqa: BLE001
            self.toolbox_label.setText(f"Toolbox: check failed ({e})")
            self._missing = []

    def install_packages(self):
        if not getattr(self, "_missing", None):
            return
        self.install_btn.setEnabled(False)
        self.log_line("Installing: " + ", ".join(self._missing))
        self._inst = InstallWorker(self._missing)
        self._inst.done.connect(self._install_done)
        self._inst.start()

    def _install_done(self, ok):
        self.log_line("Package install " + ("done." if ok else "FAILED."))
        self.refresh_toolbox()

    # ------------------------------------------------------------ queue
    def _add_paths(self, paths):
        for p in paths:
            try:
                probe = engine.probe_rom(str(p))
            except Exception as e:  # noqa: BLE001
                self.log_line(f"skip {p}: {e}")
                continue
            if not probe["ok"]:
                self.log_line(f"skip {p}: " +
                              "; ".join(probe["reasons"]))
                continue
            self.items.append(probe)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, probe,
                           "needs review" if probe["needs_review"]
                           else "ready")
        self.table.resizeColumnsToContents()

    def _fill_row(self, row, probe, status):
        vals = [probe["title"], probe["game_code"],
                probe["save_type"], f"{probe['size_mb']} MB",
                probe["region"] or "—", status]
        for c, val in enumerate(vals):
            self.table.setItem(row, c, QTableWidgetItem(val))

    def _set_status(self, row, status):
        self.table.setItem(row, STATUS_COL, QTableWidgetItem(status))

    def add_roms(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Game Boy Advance ROMs", "",
            "Game Boy Advance ROMs (*.gba *.GBA)")
        self._add_paths(paths)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of ROMs")
        if d:
            paths = sorted(Path(d).glob("*.gba")) + \
                sorted(Path(d).glob("*.GBA"))
            self._add_paths(paths)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.items[r]

    def clear_queue(self):
        self.table.setRowCount(0)
        self.items.clear()

    def show_selected(self):
        rows = self.table.selectedIndexes()
        if not rows:
            return
        probe = self.items[rows[0].row()]
        self.p_title.setText(probe["header_title"] or probe["title"])
        self.p_code.setText(
            f"{probe['game_code']} (maker {probe['maker_code']})")
        self.p_save.setText(
            probe["save_type"] +
            (f" — {probe['save_signature']}" if probe["save_signature"]
             else ""))
        self.p_sizes.setText(f"{probe['size_mb']} MB")
        self.p_region.setText(probe["region"] or "—")
        self.p_crc.setText(f"{probe['crc32']} / {probe['sha1'][:12]}…")
        self.p_cover.setText("fetching art…")
        self.cover_worker = CoverWorker(probe["title"])
        self.cover_worker.done.connect(self._cover_done)
        self.cover_worker.start()

    def _cover_done(self, title, data):
        rows = self.table.selectedIndexes()
        if not rows or self.items[rows[0].row()]["title"] != title:
            return
        if data:
            pix = QPixmap()
            if pix.loadFromData(data):
                self.p_cover.setPixmap(pix.scaled(
                    160, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self.p_cover.setText("(no art found)")

    # ------------------------------------------------------------ run
    def current_settings(self):
        return {
            "output_dir": self.s_out.text().strip(),
            "ra": self.s_ra.isChecked(),
            "ra_popups": self.s_popups.isChecked(),
            "ra_toml": self.s_toml.text().strip() or None,
            "netplay": self.s_netplay.isChecked(),
            "targets": [t for t, on in
                        [("linux", self.s_linux.isChecked()),
                         ("windows", self.s_windows.isChecked())] if on],
            "jobs": self.s_jobs.value(),
        }

    def run_queue(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "GBARecomp Tools",
                                    "Add at least one ROM first.")
            return
        s = self.current_settings()
        if not s["targets"]:
            QMessageBox.information(self, "GBARecomp Tools",
                                    "Pick at least one build target.")
            return
        self._save_settings(s)
        jobs = []
        for row, probe in enumerate(self.items):
            job = dict(s)
            job["rom"] = probe["path"]
            job["title"] = probe["title"]
            jobs.append((row, job))
        self.run_btn.setEnabled(False)
        self.worker = Worker(jobs, s)
        self.worker.log.connect(self.log_line)
        self.worker.status.connect(self._set_status)
        self.worker.finished_all.connect(self._run_done)
        self.worker.start()

    def _run_done(self):
        self.run_btn.setEnabled(True)
        self.log_line("Queue finished.")

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            s = json.loads(SETTINGS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return
        self.s_ra.setChecked(s.get("ra", True))
        self.s_popups.setChecked(s.get("ra_popups", True))
        self.s_netplay.setChecked(s.get("netplay", True))
        if s.get("ra_toml"):
            self.s_toml.setText(s["ra_toml"])
        self.s_linux.setChecked(
            "linux" in s.get("targets", ["linux", "windows"]))
        self.s_windows.setChecked(
            "windows" in s.get("targets", ["linux", "windows"]))
        self.s_jobs.setValue(int(s.get("jobs", 2)))
        if s.get("output_dir"):
            self.s_out.setText(s["output_dir"])

    def _save_settings(self, s=None):
        try:
            SETTINGS_FILE.write_text(
                json.dumps(s or self.current_settings(), indent=2))
        except Exception:  # noqa: BLE001
            pass


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
