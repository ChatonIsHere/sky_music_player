"""
Sky Music Player – GUI
A graphical interface for playing Sky: Children of the Light music sheets
with configurable global hotkeys for in-game control.

Reads song files (.txt / .json / .skysheet) directly from the library folder.
All files are decoded as JSON at runtime — no conversion step needed.
"""

import re as _re
import tkinter as tk
from tkinter import ttk
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pynput.keyboard import Controller as KbController, Key, Listener, KeyCode
import pygetwindow

# ──────────────────────────────────────────────────────────────
#  Sky instrument key mapping
# ──────────────────────────────────────────────────────────────

_BASE = {
    0: 'y', 1: 'u', 2: 'i', 3: 'o', 4: 'p',
    5: 'h', 6: 'j', 7: 'k', 8: 'l', 9: ';',
    10: 'n', 11: 'm', 12: ',', 13: '.', 14: '/',
}
SKY_KEY_MAP = {}
for _pfx in ('1Key', '2Key'):
    for _n, _c in _BASE.items():
        SKY_KEY_MAP[f'{_pfx}{_n}'] = _c

# Supported song file extensions (all contain JSON)
_SONG_EXTS = {".txt", ".json", ".skysheet"}

# ──────────────────────────────────────────────────────────────
#  Paths & config helpers
# ──────────────────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(_DIR, "hotkeys.json")

# Store generated data in %LOCALAPPDATA%\SkyMusicPlayer
_APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SkyMusicPlayer")
os.makedirs(_APP_DIR, exist_ok=True)

_DATA_DIR     = os.path.join(_APP_DIR, "_data")
os.makedirs(_DATA_DIR, exist_ok=True)

# Repository & imported songs
_REPO_URL       = "https://github.com/Ai-Vonie/Sky1984-Sheets-Collection.git"
_REPO_DIR       = os.path.join(_APP_DIR, "_sheets_repo")
_REPO_SONGS_DIR = os.path.join(_REPO_DIR, "Songs")
_IMPORTED_DIR   = os.path.join(_APP_DIR, "_imported")


DEFAULT_HOTKEYS = {
    "play_pause":   "f6",
    "next_song":    "f8",
    "prev_song":    "f7",
    "stop":         "f9",
    "cursor_up":    "up",
    "cursor_down":  "down",
    "add_to_queue": "f12",
}


def load_hotkeys():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_HOTKEYS.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return DEFAULT_HOTKEYS.copy()


def save_hotkeys(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def pynput_key_to_str(key):
    if isinstance(key, Key):
        return key.name
    if isinstance(key, KeyCode):
        if key.char is not None:
            return key.char
        if key.vk is not None:
            return f"<{key.vk}>"
    return str(key)


def str_to_pynput_key(s):
    if hasattr(Key, s):
        return getattr(Key, s)
    if s.startswith("<") and s.endswith(">"):
        return KeyCode.from_vk(int(s[1:-1]))
    return KeyCode(char=s)


def key_display(s):
    if hasattr(Key, s):
        return s.replace("_", " ").upper()
    if s.startswith("<") and s.endswith(">"):
        return f"VK {s[1:-1]}"
    return s.upper()


# ──────────────────────────────────────────────────────────────
#  Window focus helper
# ──────────────────────────────────────────────────────────────

def _foreground_window_title() -> str:
    """Return the title of the current foreground window (Windows only)."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────
#  Song loading
# ──────────────────────────────────────────────────────────────

def load_song(abs_path: str):
    """
    Read a song file (any supported extension) and decode it as JSON.
    Returns (song_dict, error_str).  song_dict has at least 'songNotes'.
    """
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, str(e)
    if isinstance(data, list) and data:
        entry = data[0]
    elif isinstance(data, dict):
        entry = data
    else:
        return None, "Unexpected file format"
    if "songNotes" not in entry or not entry["songNotes"]:
        return None, "No notes found in file"
    return entry, None


# ──────────────────────────────────────────────────────────────
#  Duration cache  (SQLite)
#
#  Persists song durations across app restarts so we only need
#  to parse each JSON file once (or again if its mtime changes).
# ──────────────────────────────────────────────────────────────

_DURATION_DB = os.path.join(_DATA_DIR, "duration_cache.db")


class DurationCache:
    """SQLite-backed cache mapping (file_path, mtime) -> duration_seconds."""

    def __init__(self, db_path: str = _DURATION_DB):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS durations ("
            "  path TEXT PRIMARY KEY,"
            "  mtime REAL NOT NULL,"
            "  duration REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, abs_path: str, mtime: float) -> float | None:
        """Return cached duration if path+mtime match, else None."""
        row = self._conn.execute(
            "SELECT duration FROM durations WHERE path = ? AND mtime = ?",
            (abs_path, mtime),
        ).fetchone()
        return row[0] if row else None

    def put(self, abs_path: str, mtime: float, duration: float):
        self._conn.execute(
            "INSERT OR REPLACE INTO durations (path, mtime, duration) VALUES (?, ?, ?)",
            (abs_path, mtime, duration),
        )

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def _song_duration(abs_path: str) -> float | None:
    """Parse a song file and return its duration in seconds, or None on error."""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    entry = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
    if not entry or "songNotes" not in entry or not entry["songNotes"]:
        return None
    try:
        return float(entry["songNotes"][-1]["time"]) / 1000.0
    except (ValueError, TypeError, KeyError):
        return None


# ──────────────────────────────────────────────────────────────
#  Favourites database  (SQLite)
# ──────────────────────────────────────────────────────────────

_FAVOURITES_DB = os.path.join(_DATA_DIR, "favourites.db")


class FavouritesDB:
    """SQLite store for favourite songs (from library or imported)."""

    def __init__(self, db_path: str = _FAVOURITES_DB):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS favourites ("
            "  path TEXT PRIMARY KEY,"
            "  source TEXT NOT NULL,"
            "  display TEXT NOT NULL,"
            "  duration REAL"
            ")"
        )
        self._conn.commit()

    def add(self, abs_path: str, source: str, display: str,
            duration: float | None):
        self._conn.execute(
            "INSERT OR IGNORE INTO favourites (path, source, display, duration) "
            "VALUES (?, ?, ?, ?)",
            (abs_path, source, display, duration),
        )
        self._conn.commit()

    def remove(self, abs_path: str):
        self._conn.execute("DELETE FROM favourites WHERE path = ?",
                           (abs_path,))
        self._conn.commit()

    def is_favourite(self, abs_path: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM favourites WHERE path = ?", (abs_path,)
        ).fetchone()
        return row is not None

    @property
    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM favourites").fetchone()
        return row[0] if row else 0

    def search(self, query: str) -> list[tuple[str, str, float | None]]:
        """Return [(display, path, duration)] matching query."""
        rows = self._conn.execute(
            "SELECT display, path, duration FROM favourites ORDER BY display"
        ).fetchall()
        if not query.strip():
            return rows
        terms = query.strip().lower().split()
        return [r for r in rows
                if all(t in r[0].lower() or t in r[1].lower() for t in terms)]

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
#  Library cache
#
#  Scans a folder in a background thread and stores
#  (display_name, search_key, abs_path, duration) tuples.
#  Durations are persisted in an SQLite DB so only new/changed
#  files need to be parsed.  Searching filters the pre-built
#  list by substring match on the lowercased relative path —
#  no filesystem access needed.
# ──────────────────────────────────────────────────────────────

class LibraryCache:
    """Thread-safe cached index of all song files in a folder."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list[tuple[str, str, str, float | None]] = []
        self._library_dir: str = ""
        self._scanning = False
        self._scan_gen: int = 0
        self._on_ready = None
        self._on_progress = None
        self._dur_cache = DurationCache()

    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def set_ready_callback(self, cb):
        self._on_ready = cb

    def set_progress_callback(self, cb):
        self._on_progress = cb

    def rescan(self, library_dir: str):
        self._library_dir = library_dir
        self._scan_gen += 1
        if not self._scanning:
            t = threading.Thread(target=self._scan_worker, daemon=True)
            t.start()

    def search(self, query: str) -> list[tuple[str, str, float | None]]:
        """Return [(display_name, abs_path, duration)] matching query."""
        q = query.strip().lower()
        with self._lock:
            if not q:
                return [(d, p, dur) for d, _sk, p, dur in self._entries]
            terms = q.split()
            return [
                (d, p, dur) for d, sk, p, dur in self._entries
                if all(t in sk for t in terms)
            ]

    def close(self):
        self._dur_cache.close()

    # ── internal ──────────────────────────────────────────────

    def _scan_worker(self):
        self._scanning = True
        try:
            while True:
                gen = self._scan_gen
                entries = self._do_scan(self._library_dir, self._dur_cache)
                with self._lock:
                    self._entries = entries
                if self._scan_gen == gen:
                    break
        finally:
            self._scanning = False
            if self._on_ready:
                self._on_ready()

    _SCAN_WORKERS = 4

    def _do_scan(self, library_dir: str, dur_cache: DurationCache
                 ) -> list[tuple[str, str, str, float | None]]:
        if not library_dir or not os.path.isdir(library_dir):
            return []

        all_files: list[tuple[str, str, str]] = []
        for root, _dirs, files in os.walk(library_dir):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SONG_EXTS:
                    continue
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, library_dir)
                all_files.append((abs_path, rel_path, fname))

        total = len(all_files)
        progress_cb = self._on_progress
        if progress_cb:
            progress_cb(0, total)

        cached_results: list[tuple[int, str, str, str, float | None]] = []
        to_parse: list[tuple[int, str, str, str]] = []

        for idx, (abs_path, rel_path, fname) in enumerate(all_files):
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                mtime = 0.0
            duration = dur_cache.get(abs_path, mtime)
            if duration is not None:
                display = os.path.splitext(fname)[0]
                cached_results.append(
                    (idx, display, rel_path.lower(), abs_path, duration))
            else:
                to_parse.append((idx, abs_path, rel_path, fname))

        results: list[tuple[str, str, str, float | None] | None] = [None] * total
        done_count = 0

        for idx, display, search_key, abs_path, duration in cached_results:
            results[idx] = (display, search_key, abs_path, duration)
            done_count += 1

        if progress_cb and done_count > 0:
            progress_cb(done_count, total)

        def _parse_one(item):
            idx, abs_path, rel_path, fname = item
            display = os.path.splitext(fname)[0]
            search_key = rel_path.lower()
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                mtime = 0.0
            duration = _song_duration(abs_path)
            return idx, display, search_key, abs_path, mtime, duration

        if to_parse:
            with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as pool:
                futures = {pool.submit(_parse_one, item): item
                           for item in to_parse}
                for future in as_completed(futures):
                    idx, display, search_key, abs_path, mtime, duration = \
                        future.result()
                    results[idx] = (display, search_key, abs_path, duration)
                    if duration is not None:
                        dur_cache.put(abs_path, mtime, duration)
                    done_count += 1
                    if progress_cb and done_count % 50 == 0:
                        progress_cb(done_count, total)

        dur_cache.commit()
        if progress_cb:
            progress_cb(total, total)
        return [r for r in results if r is not None]


# ──────────────────────────────────────────────────────────────
#  Playback engine  (background thread, absolute-time scheduling)
# ──────────────────────────────────────────────────────────────

class PlaybackEngine:
    def __init__(self):
        self.keyboard = KbController()
        self._thread = None
        self._stop_ev = threading.Event()
        self._pause_ev = threading.Event()
        self.playing = False
        self.paused = False
        self.current_time = 0.0
        self.total_time = 0.0
        self.song_name = ""
        self._on_finish = None
        self._notes: list = []

    def play(self, song_dict, on_finish=None):
        self.stop()
        self._stop_ev.clear()
        self._pause_ev.clear()
        self._on_finish = on_finish
        notes = song_dict["songNotes"]
        self._notes = list(notes)
        self.song_name = song_dict.get("name", "Unknown")
        self.total_time = float(notes[-1]["time"]) / 1000.0 if notes else 0
        self.current_time = 0.0
        self.playing = True
        self.paused = False
        self._thread = threading.Thread(
            target=self._loop, args=(self._notes, 0.0), daemon=True
        )
        self._thread.start()

    def seek(self, position_seconds: float):
        if not self._notes:
            return
        was_paused = self.paused
        position_seconds = max(0.0, min(position_seconds, self.total_time))
        target_ms = position_seconds * 1000.0
        start_idx = 0
        for i, note in enumerate(self._notes):
            if float(note["time"]) >= target_ms:
                start_idx = i
                break
        else:
            start_idx = len(self._notes) - 1
        self._stop_ev.set()
        self._pause_ev.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._stop_ev.clear()
        if was_paused:
            self._pause_ev.set()
        self.current_time = position_seconds
        self.playing = True
        self.paused = was_paused
        self._thread = threading.Thread(
            target=self._loop,
            args=(self._notes[start_idx:], position_seconds),
            daemon=True,
        )
        self._thread.start()

    def toggle_pause(self):
        if not self.playing:
            return
        if self.paused:
            self._pause_ev.clear()
            self.paused = False
        else:
            self._pause_ev.set()
            self.paused = True

    def stop(self):
        self._stop_ev.set()
        self._pause_ev.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.playing = False
        self.paused = False
        self.current_time = 0.0
        self._notes = []

    @staticmethod
    def _sky_window():
        try:
            for w in pygetwindow.getWindowsWithTitle("Sky"):
                if w.title == "Sky":
                    return w
        except Exception:
            pass
        return None

    def _press(self, key_name):
        ch = SKY_KEY_MAP.get(key_name)
        if ch:
            self.keyboard.press(ch)
            time.sleep(0.02)
            self.keyboard.release(ch)

    def _wait_until(self, target_wall_time):
        while True:
            if self._stop_ev.is_set():
                return target_wall_time
            if self._pause_ev.is_set():
                pause_start = time.perf_counter()
                while self._pause_ev.is_set() and not self._stop_ev.is_set():
                    time.sleep(0.05)
                target_wall_time += time.perf_counter() - pause_start
                if self._stop_ev.is_set():
                    return target_wall_time
            now = time.perf_counter()
            remaining = target_wall_time - now
            if remaining <= 0:
                return target_wall_time
            if remaining > 0.015:
                time.sleep(min(remaining - 0.010, 0.050))
            else:
                while time.perf_counter() < target_wall_time:
                    pass
                return target_wall_time

    def _loop(self, notes, time_offset: float = 0.0):
        sky = self._sky_window()
        t0 = time.perf_counter() - time_offset
        try:
            for note in notes:
                if self._stop_ev.is_set():
                    break
                note_target_s = float(note["time"]) / 1000.0
                target_wall = t0 + note_target_s
                target_wall = self._wait_until(target_wall)
                t0 = target_wall - note_target_s
                if self._stop_ev.is_set():
                    break
                if sky:
                    try:
                        if not sky.isActive:
                            focus_start = time.perf_counter()
                            while not sky.isActive and not self._stop_ev.is_set():
                                time.sleep(0.15)
                            t0 += time.perf_counter() - focus_start
                    except Exception:
                        sky = self._sky_window()
                if self._stop_ev.is_set():
                    break
                self._press(note["key"])
                self.current_time = time.perf_counter() - t0
            if not self._stop_ev.is_set():
                self.current_time = self.total_time
        finally:
            self.playing = False
            self.paused = False
            if self._on_finish and not self._stop_ev.is_set():
                self._on_finish()


# ──────────────────────────────────────────────────────────────
#  Colour palette & styling
# ──────────────────────────────────────────────────────────────

COL_BG        = "#1e1e2e"
COL_SURFACE   = "#282840"
COL_ACCENT    = "#7c6fe0"
COL_ACCENT_LT = "#9d93e8"
COL_TEXT      = "#e0def4"
COL_TEXT_DIM  = "#6e6a86"
COL_SUCCESS   = "#a6e3a1"
COL_WARN      = "#f9e2af"
COL_ERR       = "#f38ba8"
COL_NOW_PLAY  = "#3a3560"
COL_LIST_BG   = "#232338"
COL_LIST_SEL  = "#44407a"
COL_BTN_BG    = "#3b3660"
COL_BTN_FG    = "#e0def4"
COL_TAB_ACTIVE = COL_ACCENT
COL_TAB_INACTIVE = COL_BTN_BG

FONT_MAIN   = ("Segoe UI", 10)
FONT_SMALL  = ("Segoe UI", 9)
FONT_TINY   = ("Segoe UI", 8)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_HOTKEY = ("Segoe UI Semibold", 9)


# ──────────────────────────────────────────────────────────────
#  Queue manager
# ──────────────────────────────────────────────────────────────

class QueueManager:
    def __init__(self):
        self.items: list = []     # [(display_name, abs_path)]
        self._index: int = -1

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def current(self):
        if 0 <= self._index < len(self.items):
            return self.items[self._index]
        return None

    @property
    def has_next(self) -> bool:
        return self._index < len(self.items) - 1

    @property
    def has_prev(self) -> bool:
        return self._index > 0

    @property
    def is_active(self) -> bool:
        return self._index >= 0

    def __len__(self):
        return len(self.items)

    def add(self, display: str, abs_path: str):
        self.items.append((display, abs_path))

    def remove(self, idx: int):
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            if idx < self._index:
                self._index -= 1
            elif idx == self._index:
                if self._index >= len(self.items):
                    self._index = len(self.items) - 1

    def pop_current(self):
        """Remove the currently-playing item. Index stays so it points to the
        next song (which slid into this position)."""
        if 0 <= self._index < len(self.items):
            self.items.pop(self._index)
            if self._index >= len(self.items):
                self._index = -1

    def clear(self):
        self.items.clear()
        self._index = -1

    def move_up(self, idx: int):
        if idx <= 0 or idx >= len(self.items):
            return
        self.items[idx - 1], self.items[idx] = self.items[idx], self.items[idx - 1]
        if self._index == idx:
            self._index -= 1
        elif self._index == idx - 1:
            self._index += 1

    def move_down(self, idx: int):
        if idx < 0 or idx >= len(self.items) - 1:
            return
        self.items[idx], self.items[idx + 1] = self.items[idx + 1], self.items[idx]
        if self._index == idx:
            self._index += 1
        elif self._index == idx + 1:
            self._index -= 1

    def start(self):
        if not self.items:
            return None
        self._index = 0
        return self.items[0]

    def advance(self):
        if self._index < len(self.items) - 1:
            self._index += 1
            return self.items[self._index]
        return None

    def go_back(self):
        if self._index > 0:
            self._index -= 1
            return self.items[self._index]
        return None

    def reset(self):
        self._index = -1


# ──────────────────────────────────────────────────────────────
#  GUI
# ──────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sky Music Player")
        self.configure(bg=COL_BG)
        self.minsize(540, 780)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.engine = PlaybackEngine()
        self.hotkeys = load_hotkeys()
        self.queue = QueueManager()

        # ── caches ────────────────────────────────────────────
        self._lib_cache = LibraryCache()
        self._lib_cache.set_ready_callback(
            lambda: self.after(0, self._on_lib_ready))
        self._lib_cache.set_progress_callback(self._on_scan_progress)

        self._yoursongs_cache = LibraryCache()
        self._yoursongs_cache.set_ready_callback(
            lambda: self.after(0, self._on_yoursongs_ready))
        self._yoursongs_cache.set_progress_callback(self._on_scan_progress)

        self._fav_db = FavouritesDB()

        # ── tab state ─────────────────────────────────────────
        self._active_tab: str = "library"
        self._tab_btns: dict[str, tk.Button] = {}
        self._tab_frames: dict[str, tk.Frame] = {}
        self._visible: dict[str, list] = {
            "favourites": [], "library": [], "yoursongs": []}
        self._search_vars: dict[str, tk.StringVar] = {}
        self._listboxes: dict[str, tk.Listbox] = {}
        self._status_labels: dict[str, tk.Label] = {}
        self._duration_filters: dict[str, tk.StringVar] = {}
        self._debounce_ids: dict[str, str | None] = {
            "favourites": None, "library": None, "yoursongs": None}

        # ── misc state ────────────────────────────────────────
        self._capturing = None
        self._hotkey_labels: dict = {}
        self._hotkey_keys: dict = {}
        self._seeking = False
        self._scan_overlay = None
        self._pending_scans = 0
        self._hotkey_win = None
        self._sky_open = False
        self._sky_focused = False

        self._apply_theme()
        self._build_ui()
        self._resolve_hotkeys()

        # ── kick off repo setup + scans ───────────────────────
        self._pending_scans = 2  # library (via repo) + imported
        self._show_scan_overlay("Setting up Library",
                                "Checking repository\u2026")
        threading.Thread(target=self._setup_repo, daemon=True).start()

        # your songs — scan immediately
        os.makedirs(_IMPORTED_DIR, exist_ok=True)
        self._yoursongs_cache.rescan(_IMPORTED_DIR)

        self._refresh_fav_list()

        # ── global hotkey listener ────────────────────────────
        self._listener = Listener(on_press=self._on_global_key)
        self._listener.daemon = True
        self._listener.start()

        # ── periodic ticks ────────────────────────────────────
        self._tick_progress()
        self._tick_sky()

    # ══════════════════════════════════════════════════════════
    #  Theme
    # ══════════════════════════════════════════════════════════

    def _apply_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=COL_BG, foreground=COL_TEXT,
                         font=FONT_MAIN, borderwidth=0)
        style.configure("TFrame", background=COL_BG)
        style.configure("TLabel", background=COL_BG, foreground=COL_TEXT)
        style.configure("TScale", background=COL_BG, troughcolor=COL_SURFACE,
                         sliderthickness=14)
        style.configure("Horizontal.TScale", background=COL_BG)

    # ══════════════════════════════════════════════════════════
    #  Build UI
    # ══════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── header ────────────────────────────────────────────
        hdr = tk.Frame(self, bg=COL_SURFACE, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Sky Music Player",
                 font=("Segoe UI", 13, "bold"),
                 bg=COL_SURFACE, fg=COL_ACCENT_LT
                 ).pack(side="left", padx=14, pady=6)
        self.lbl_sky = tk.Label(hdr, text="Sky: \u2026", font=FONT_TINY,
                                bg=COL_SURFACE, fg=COL_TEXT_DIM)
        self.lbl_sky.pack(side="right", padx=14)
        self._make_btn(hdr, "\u2699 Hotkeys", self._show_hotkey_window,
                        side="right", padx=(0, 6), small=True)

        body = tk.Frame(self, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # ── songs section (LabelFrame) ─────────────────────────
        songs_frm = tk.LabelFrame(body, text="  Songs  ", font=FONT_BOLD,
                                   bg=COL_BG, fg=COL_ACCENT_LT, bd=1,
                                   highlightbackground=COL_BG,
                                   highlightthickness=0, padx=8, pady=6)
        songs_frm.pack(fill="both", expand=True, pady=(0, 6))

        # ── tab bar ───────────────────────────────────────────
        tab_bar = tk.Frame(songs_frm, bg=COL_BG)
        tab_bar.pack(fill="x", pady=(0, 4))
        for tab_id, label in [("favourites", "\u2605 Favourites"),
                               ("library", "Library"),
                               ("yoursongs", "Your Songs")]:
            btn = tk.Button(
                tab_bar, text=label, font=FONT_SMALL,
                bg=COL_TAB_INACTIVE, fg=COL_BTN_FG,
                activebackground=COL_ACCENT_LT, activeforeground="#fff",
                bd=0, relief="flat", padx=14, pady=5,
                command=lambda t=tab_id: self._switch_tab(t))
            btn.pack(side="left", padx=(0, 2))
            self._tab_btns[tab_id] = btn

        # ── tab content container ─────────────────────────────
        self._tab_container = tk.Frame(songs_frm, bg=COL_BG)
        self._tab_container.pack(fill="both", expand=True)

        self._build_song_tab("favourites", self._tab_container)
        self._build_song_tab("library", self._tab_container,
                             toolbar_btns=[
                                 ("Sync", self._sync_library),
                             ])
        self._build_song_tab("yoursongs", self._tab_container,
                             toolbar_btns=[
                                 ("Import", self._import_songs),
                             ])
        self._switch_tab("favourites")

        # ── queue (above transport) ───────────────────────────
        self._build_queue(body)
        # ── transport ─────────────────────────────────────────
        self._build_transport(body)

    # ···· per-tab song list ···································

    def _build_song_tab(self, tab_id: str, parent, *, toolbar_btns=None):
        frm = tk.Frame(parent, bg=COL_BG)
        self._tab_frames[tab_id] = frm  # don't pack — _switch_tab handles it

        # toolbar row 1: search + buttons
        tb = tk.Frame(frm, bg=COL_BG)
        tb.pack(fill="x", pady=(0, 2))
        tk.Label(tb, text="Search:", font=FONT_SMALL,
                 bg=COL_BG, fg=COL_TEXT).pack(side="left")
        sv = tk.StringVar()
        sv.trace_add("write",
                     lambda *_, t=tab_id: self._on_tab_search_changed(t))
        self._search_vars[tab_id] = sv
        tk.Entry(tb, textvariable=sv, font=FONT_SMALL,
                 bg=COL_LIST_BG, fg=COL_TEXT,
                 insertbackground=COL_TEXT, bd=0, relief="flat"
                 ).pack(side="left", fill="x", expand=True, padx=(6, 6))

        if toolbar_btns:
            for text, cmd in reversed(toolbar_btns):
                self._make_btn(tb, text, cmd, side="right", padx=(0, 4))

        # toolbar row 2: duration filter
        fb = tk.Frame(frm, bg=COL_BG)
        fb.pack(fill="x", pady=(0, 4))
        tk.Label(fb, text="Duration:", font=FONT_TINY,
                 bg=COL_BG, fg=COL_TEXT_DIM).pack(side="left")
        dv = tk.StringVar(value="All")
        dv.trace_add("write",
                     lambda *_, t=tab_id: self._on_tab_search_changed(t))
        self._duration_filters[tab_id] = dv
        for lbl in ["All", "<30s", ">30s", "30s-1m", "1-2m", "2-5m", "5m+"]:
            tk.Radiobutton(
                fb, text=lbl, variable=dv, value=lbl,
                font=FONT_TINY, bg=COL_BG, fg=COL_TEXT_DIM,
                selectcolor=COL_SURFACE, activebackground=COL_BG,
                activeforeground=COL_ACCENT_LT, indicatoron=False,
                bd=0, relief="flat", padx=6, pady=1,
            ).pack(side="left", padx=(4, 0))

        # listbox
        lf = tk.Frame(frm, bg=COL_BG)
        lf.pack(fill="both", expand=True)
        lb = tk.Listbox(
            lf, height=10, activestyle="none", selectmode="browse",
            font=FONT_MAIN, bg=COL_LIST_BG, fg=COL_TEXT,
            selectbackground=COL_LIST_SEL, selectforeground="#fff",
            highlightthickness=0, bd=0, relief="flat",
        )
        sb = tk.Scrollbar(lf, orient="vertical", command=lb.yview,
                          bg=COL_SURFACE, troughcolor=COL_LIST_BG,
                          highlightthickness=0, bd=0)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        lb.bind("<Double-1>", lambda _, t=tab_id: self._add_to_queue(t))
        # Suppress default arrow-key handling so the global hotkeys
        # don't double-move the selection.
        lb.bind("<Up>", lambda e: "break")
        lb.bind("<Down>", lambda e: "break")
        self._listboxes[tab_id] = lb

        # bottom row: status label + action buttons
        bot = tk.Frame(frm, bg=COL_BG)
        bot.pack(fill="x", pady=(4, 0))
        sl = tk.Label(bot, text="", font=FONT_TINY, bg=COL_BG,
                      fg=COL_TEXT_DIM)
        sl.pack(side="left")
        self._status_labels[tab_id] = sl
        if tab_id != "favourites":
            self._make_btn(bot, "\u2605 Favourite",
                           lambda t=tab_id: self._toggle_favourite(t),
                           side="right", padx=(4, 0), small=True)
        else:
            self._make_btn(bot, "Remove \u2605",
                           self._remove_favourite_selected,
                           side="right", padx=(4, 0), small=True)
        self._make_btn(bot, "+ Queue",
                       lambda t=tab_id: self._add_to_queue(t),
                       side="right", padx=(4, 0), small=True)

    # ···· transport bar ·······································

    def _build_transport(self, parent):
        frm = tk.Frame(parent, bg=COL_SURFACE, bd=0, highlightthickness=1,
                        highlightbackground=COL_ACCENT)
        frm.pack(fill="x", pady=(6, 0), ipady=8)

        self.lbl_now = tk.Label(frm, text="No song loaded", font=FONT_BOLD,
                                bg=COL_SURFACE, fg=COL_TEXT, anchor="w")
        self.lbl_now.pack(fill="x", padx=12, pady=(8, 2))

        scr = tk.Frame(frm, bg=COL_SURFACE)
        scr.pack(fill="x", padx=12, pady=(0, 2))

        self.lbl_time_left = tk.Label(scr, text="0:00", font=FONT_TINY,
                                      bg=COL_SURFACE, fg=COL_TEXT_DIM,
                                      width=5, anchor="w")
        self.lbl_time_left.pack(side="left")

        self.progress = ttk.Scale(scr, from_=0, to=1, orient="horizontal")
        self.progress.pack(side="left", fill="x", expand=True, padx=4)
        self.progress.bind("<ButtonPress-1>", self._seek_start)
        self.progress.bind("<ButtonRelease-1>", self._seek_end)

        self.lbl_time_right = tk.Label(scr, text="0:00", font=FONT_TINY,
                                       bg=COL_SURFACE, fg=COL_TEXT_DIM,
                                       width=5, anchor="e")
        self.lbl_time_right.pack(side="right")

        btn_row = tk.Frame(frm, bg=COL_SURFACE)
        btn_row.pack(pady=(2, 8))
        self.btn_prev = self._make_btn(btn_row, "  Prev  ", self._prev_song,
                                        side="left", padx=3)
        self.btn_play = self._make_btn(btn_row, "  Play  ", self._play_pause,
                                        side="left", padx=3, accent=True)
        self.btn_next = self._make_btn(btn_row, "  Next  ", self._next_song,
                                        side="left", padx=3)
        self.btn_stop = self._make_btn(btn_row, "  Stop  ", self._stop,
                                        side="left", padx=3)

    # ···· queue ···············································

    def _build_queue(self, parent):
        frm = tk.LabelFrame(parent, text="  Queue  ", font=FONT_BOLD,
                             bg=COL_BG, fg=COL_ACCENT_LT, bd=1,
                             highlightbackground=COL_BG,
                             highlightthickness=0, padx=8, pady=6)
        frm.pack(fill="x", pady=(0, 0))

        ql_frame = tk.Frame(frm, bg=COL_BG)
        ql_frame.pack(fill="both", expand=True)

        self.queue_list = tk.Listbox(
            ql_frame, height=5, activestyle="none", selectmode="browse",
            font=FONT_SMALL, bg=COL_LIST_BG, fg=COL_TEXT,
            selectbackground=COL_LIST_SEL, selectforeground="#fff",
            highlightthickness=0, bd=0, relief="flat",
        )
        qscroll = tk.Scrollbar(ql_frame, orient="vertical",
                                command=self.queue_list.yview,
                                bg=COL_SURFACE, troughcolor=COL_LIST_BG,
                                highlightthickness=0, bd=0)
        self.queue_list.configure(yscrollcommand=qscroll.set)
        self.queue_list.pack(side="left", fill="both", expand=True)
        qscroll.pack(side="right", fill="y")

        qbf = tk.Frame(frm, bg=COL_BG)
        qbf.pack(fill="x", pady=(6, 0))
        self._make_btn(qbf, "Up", self._queue_move_up,
                        side="left", padx=2, small=True)
        self._make_btn(qbf, "Down", self._queue_move_down,
                        side="left", padx=2, small=True)
        self._make_btn(qbf, "Remove", self._remove_from_queue,
                        side="left", padx=2, small=True)
        self._make_btn(qbf, "Clear", self._clear_queue,
                        side="left", padx=2, small=True)
        self.lbl_queue = tk.Label(qbf, text="Empty", font=FONT_TINY,
                                  bg=COL_BG, fg=COL_TEXT_DIM)
        self.lbl_queue.pack(side="right")

    # ···· hotkeys popup ·······································

    def _show_hotkey_window(self):
        if self._hotkey_win is not None:
            try:
                self._hotkey_win.lift()
                self._hotkey_win.focus_force()
                return
            except tk.TclError:
                self._hotkey_win = None

        win = tk.Toplevel(self)
        win.title("Hotkeys")
        win.configure(bg=COL_BG)
        win.resizable(False, False)
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", self._close_hotkey_window)
        self._hotkey_win = win

        tk.Label(win, text="Hotkey Configuration", font=FONT_BOLD,
                 bg=COL_BG, fg=COL_TEXT).pack(padx=16, pady=(12, 8))

        frm = tk.Frame(win, bg=COL_BG)
        frm.pack(padx=16, pady=(0, 12))

        self._hotkey_labels.clear()
        for row, (action, label) in enumerate([
            ("play_pause",   "Play / Pause"),
            ("next_song",    "Next"),
            ("prev_song",    "Previous"),
            ("stop",         "Stop"),
            ("cursor_up",    "Cursor Up"),
            ("cursor_down",  "Cursor Down"),
            ("add_to_queue", "Add to Queue"),
        ]):
            tk.Label(frm, text=label, font=FONT_SMALL, bg=COL_BG,
                     fg=COL_TEXT, width=14, anchor="w"
                     ).grid(row=row, column=0, sticky="w", pady=1)
            kl = tk.Label(
                frm, text=key_display(self.hotkeys[action]),
                width=14, font=FONT_HOTKEY, bg=COL_SURFACE,
                fg=COL_ACCENT_LT, anchor="center", relief="flat",
                padx=6, pady=2,
            )
            kl.grid(row=row, column=1, padx=(8, 6), pady=1)
            self._hotkey_labels[action] = kl
            tk.Button(
                frm, text="Set", font=FONT_TINY, width=4,
                bg=COL_BTN_BG, fg=COL_BTN_FG, bd=0, relief="flat",
                activebackground=COL_ACCENT, activeforeground="#fff",
                command=lambda a=action: self._start_capture(a),
            ).grid(row=row, column=2, pady=1)

        # Position on same monitor as main window
        win.update_idletasks()
        mx = self.winfo_x()
        my = self.winfo_y()
        mw = self.winfo_width()
        mh = self.winfo_height()
        ww = win.winfo_reqwidth()
        wh = win.winfo_reqheight()
        x = mx + (mw - ww) // 2
        y = my + (mh - wh) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close_hotkey_window(self):
        if self._hotkey_win:
            self._hotkey_win.destroy()
            self._hotkey_win = None

    # ── helper: themed button ────────────────────────────────

    def _make_btn(self, parent, text, cmd, side="left", padx=0,
                  accent=False, small=False):
        font = FONT_TINY if small else FONT_SMALL
        bg = COL_ACCENT if accent else COL_BTN_BG
        fg = "#fff" if accent else COL_BTN_FG
        btn = tk.Button(parent, text=text, font=font, bg=bg, fg=fg,
                        activebackground=COL_ACCENT_LT,
                        activeforeground="#fff",
                        bd=0, relief="flat", padx=8, pady=3, command=cmd)
        btn.pack(side=side, padx=padx)
        return btn

    # ══════════════════════════════════════════════════════════
    #  Tab switching
    # ══════════════════════════════════════════════════════════

    def _switch_tab(self, tab_id: str):
        self._active_tab = tab_id
        for tid, frm in self._tab_frames.items():
            if tid == tab_id:
                frm.pack(in_=self._tab_container,
                         fill="both", expand=True)
            else:
                frm.pack_forget()
        for tid, btn in self._tab_btns.items():
            if tid == tab_id:
                btn.config(bg=COL_TAB_ACTIVE, fg="#fff")
            else:
                btn.config(bg=COL_TAB_INACTIVE, fg=COL_BTN_FG)

    # ══════════════════════════════════════════════════════════
    #  Repository management
    # ══════════════════════════════════════════════════════════

    def _setup_repo(self):
        """Clone or update the sheets repo, then trigger library scan."""
        # Check git is available
        try:
            subprocess.run(["git", "--version"],
                           capture_output=True, check=True, timeout=10)
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            self._pending_scans = max(0, self._pending_scans - 1)
            self.after(0, lambda: self._status_labels["library"].config(
                text="Git not found \u2014 install Git to download the"
                     " song library",
                fg=COL_ERR))
            self.after(0, self._hide_scan_overlay_if_done)
            return

        try:
            if os.path.isdir(os.path.join(_REPO_DIR, ".git")):
                # Existing clone — check for updates
                self.after(0, lambda: self._update_overlay_text(
                    "Checking for updates\u2026"))
                if self._check_repo_update():
                    self.after(0, lambda: self._update_overlay_text(
                        "Updating library\u2026"))
                    self._pull_repo()
            else:
                # First time — clone
                self.after(0, lambda: self._update_overlay_text(
                    "Downloading song library\u2026\n"
                    "This may take a minute."))
                self._clone_repo()
        except Exception as e:
            self._pending_scans = max(0, self._pending_scans - 1)
            err = str(e)
            self.after(0, lambda: self._status_labels["library"].config(
                text=f"Repo error: {err}", fg=COL_ERR))
            self.after(0, self._hide_scan_overlay_if_done)
            return

        # Trigger library scan
        if os.path.isdir(_REPO_SONGS_DIR):
            self.after(0, lambda: self._update_overlay_text(
                "Scanning songs\u2026"))
            self._lib_cache.rescan(_REPO_SONGS_DIR)
        else:
            self._pending_scans = max(0, self._pending_scans - 1)
            self.after(0, lambda: self._status_labels["library"].config(
                text="Songs folder not found in repository", fg=COL_ERR))
            self.after(0, self._hide_scan_overlay_if_done)

    # ── git progress streaming helper ─────────────────────

    _GIT_PCT_RE = _re.compile(r'(\d+)%')

    def _run_git_progress(self, cmd: list[str], label: str, *,
                          cwd: str | None = None, timeout: int = 600):
        """Run a git command, streaming its stderr progress to the overlay."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        buf = ""
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                ch = proc.stderr.read(1)
                if not ch:
                    break
                if ch in ('\r', '\n'):
                    line = buf.strip()
                    if line:
                        self._parse_git_line(line, label)
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                self._parse_git_line(buf.strip(), label)
        finally:
            proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd,
                output=proc.stdout.read() if proc.stdout else "",
                stderr="")

    def _parse_git_line(self, line: str, label: str):
        """Extract percentage from a git progress line and update overlay."""
        m = self._GIT_PCT_RE.search(line)
        if m:
            pct = int(m.group(1)) / 100.0
            # Build a nice short description from the line
            phase = line.split(':')[0].strip() if ':' in line else line
            # Cap phase length
            if len(phase) > 50:
                phase = phase[:47] + '\u2026'
            display = f"{label}\n{phase}: {int(pct * 100)}%"
            self.after(0, self._set_overlay_progress, display, pct)
        else:
            self.after(0, self._update_overlay_text, f"{label}\n{line[:60]}")

    def _set_overlay_progress(self, text: str, fraction: float):
        """Update both the overlay detail text and progress bar."""
        if self._scan_overlay is None:
            return
        self._scan_ov_detail.config(text=text)
        self._scan_ov_bar.place(relx=0, rely=0, relheight=1,
                                relwidth=max(0.0, min(fraction, 1.0)))
        self._scan_ov_pct.config(text=f"{int(fraction * 100)} %")

    def _clone_repo(self):
        """Shallow sparse clone of the Songs folder only."""
        try:
            self._run_git_progress([
                "git", "clone", "--depth", "1", "--progress",
                "--filter=blob:none", "--sparse",
                _REPO_URL, _REPO_DIR,
            ], "Cloning repository")
            self.after(0, self._update_overlay_text,
                       "Configuring sparse checkout\u2026")
            self._run_git_progress([
                "git", "sparse-checkout", "set", "Songs",
            ], "Downloading songs", cwd=_REPO_DIR)
        except Exception:
            # Clean up partial clone
            if os.path.isdir(_REPO_DIR):
                shutil.rmtree(_REPO_DIR, ignore_errors=True)
            raise

    def _check_repo_update(self) -> bool:
        """Return True if the remote has newer commits than local HEAD."""
        try:
            r = subprocess.run(
                ["git", "ls-remote", "origin", "refs/heads/master"],
                cwd=_REPO_DIR, capture_output=True, text=True, timeout=15)
            if r.returncode != 0 or not r.stdout.strip():
                return False
            remote_sha = r.stdout.split()[0]
            r2 = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=_REPO_DIR, capture_output=True, text=True)
            local_sha = r2.stdout.strip()
            return remote_sha != local_sha
        except Exception:
            return False

    def _pull_repo(self):
        """Fetch and reset to latest remote commit."""
        self._run_git_progress(
            ["git", "fetch", "--depth", "1", "--progress",
             "origin", "master"],
            "Fetching updates", cwd=_REPO_DIR)
        subprocess.run(
            ["git", "reset", "--hard", "origin/master"],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=60)

    def _sync_library(self):
        """Check for repo updates then rescan (single Sync button)."""
        self._pending_scans += 1
        self._show_scan_overlay("Syncing Library",
                                "Checking for updates\u2026")
        threading.Thread(target=self._sync_library_worker,
                         daemon=True).start()

    def _sync_library_worker(self):
        try:
            updated = False
            if os.path.isdir(os.path.join(_REPO_DIR, ".git")):
                if self._check_repo_update():
                    self.after(0, lambda: self._update_overlay_text(
                        "Downloading updates\u2026"))
                    self._pull_repo()
                    updated = True
                else:
                    self.after(0, lambda: self._update_overlay_text(
                        "Already up to date \u2014 rescanning\u2026"))
            else:
                self.after(0, lambda: self._update_overlay_text(
                    "Downloading song library\u2026"))
                self._clone_repo()
                updated = True

            if os.path.isdir(_REPO_SONGS_DIR):
                self._lib_cache.rescan(_REPO_SONGS_DIR)
                # _on_lib_ready decrements _pending_scans
            else:
                self._pending_scans = max(0, self._pending_scans - 1)
                self.after(0, lambda: self._status_labels["library"].config(
                    text="Songs folder not found", fg=COL_ERR))
                self.after(0, self._hide_scan_overlay_if_done)
        except Exception as e:
            self._pending_scans = max(0, self._pending_scans - 1)
            err = str(e)
            self.after(0, lambda: self._status_labels["library"].config(
                text=f"Sync error: {err}", fg=COL_ERR))
            self.after(0, self._hide_scan_overlay_if_done)

    # ══════════════════════════════════════════════════════════
    #  Your Songs folder management
    # ══════════════════════════════════════════════════════════

    def _import_songs(self):
        """Import song files into the Your Songs folder."""
        from tkinter import filedialog
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        files = filedialog.askopenfilenames(
            parent=self,
            title="Import Song Files",
            initialdir=downloads if os.path.isdir(downloads) else _DIR,
            filetypes=[
                ("Song files", "*.txt *.json *.skysheet"),
                ("All files", "*.*"),
            ],
        )
        if not files:
            return
        os.makedirs(_IMPORTED_DIR, exist_ok=True)
        count = 0
        for src in files:
            fname = os.path.basename(src)
            dest = os.path.join(_IMPORTED_DIR, fname)
            if os.path.abspath(src) == os.path.abspath(dest):
                continue
            try:
                shutil.copy2(src, dest)
                count += 1
            except Exception:
                pass
        if count > 0:
            self._status_labels["yoursongs"].config(
                text=f"Imported {count} file{'s' if count != 1 else ''}"
                     " \u2014 rescanning\u2026",
                fg=COL_SUCCESS)
            self._yoursongs_cache.rescan(imported_dir)
        else:
            self._status_labels["yoursongs"].config(
                text="No new files imported", fg=COL_TEXT_DIM)

    def _on_lib_ready(self):
        self._pending_scans = max(0, self._pending_scans - 1)
        self._hide_scan_overlay_if_done()
        self._apply_tab_search("library")

    def _on_yoursongs_ready(self):
        self._pending_scans = max(0, self._pending_scans - 1)
        self._hide_scan_overlay_if_done()
        self._apply_tab_search("yoursongs")

    # ── scan overlay ─────────────────────────────────────────

    def _show_scan_overlay(self, title="Scanning Library",
                           detail="Discovering files\u2026"):
        if self._scan_overlay is not None:
            return
        ov = tk.Frame(self, bg=COL_BG)
        ov.place(relx=0, rely=0, relwidth=1, relheight=1)
        ov.lift()

        inner = tk.Frame(ov, bg=COL_BG)
        inner.place(relx=0.5, rely=0.42, anchor="center")

        tk.Label(inner, text="\u266b", font=("Segoe UI", 48),
                 bg=COL_BG, fg=COL_ACCENT).pack()
        tk.Label(inner, text=title,
                 font=("Segoe UI", 16, "bold"), bg=COL_BG,
                 fg=COL_TEXT).pack(pady=(10, 4))
        self._scan_ov_detail = tk.Label(
            inner, text=detail,
            font=FONT_SMALL, bg=COL_BG, fg=COL_TEXT_DIM)
        self._scan_ov_detail.pack(pady=(0, 12))

        bar_bg = tk.Frame(inner, bg=COL_SURFACE, height=8, width=320)
        bar_bg.pack()
        bar_bg.pack_propagate(False)
        bar_fill = tk.Frame(bar_bg, bg=COL_ACCENT, height=8)
        bar_fill.place(relx=0, rely=0, relheight=1, relwidth=0)
        self._scan_ov_bar = bar_fill

        self._scan_ov_pct = tk.Label(
            inner, text="0 %", font=FONT_TINY, bg=COL_BG, fg=COL_TEXT_DIM)
        self._scan_ov_pct.pack(pady=(6, 0))

        self._scan_overlay = ov

    def _hide_scan_overlay(self):
        if self._scan_overlay is not None:
            self._scan_overlay.destroy()
            self._scan_overlay = None

    def _hide_scan_overlay_if_done(self):
        if self._pending_scans <= 0:
            self._hide_scan_overlay()

    def _update_overlay_text(self, detail: str):
        """Update the scan overlay detail text."""
        if self._scan_overlay is not None:
            self._scan_ov_detail.config(text=detail)

    def _on_scan_progress(self, scanned: int, total: int):
        self.after(0, self._update_scan_overlay, scanned, total)

    def _update_scan_overlay(self, scanned: int, total: int):
        if self._scan_overlay is None:
            return
        pct = scanned / total if total > 0 else 0.0
        self._scan_ov_bar.place(relx=0, rely=0, relheight=1, relwidth=pct)
        self._scan_ov_pct.config(text=f"{int(pct * 100)} %")
        self._scan_ov_detail.config(
            text=f"Processing {scanned:,} / {total:,} files\u2026")

    # ══════════════════════════════════════════════════════════
    #  Search (per-tab)
    # ══════════════════════════════════════════════════════════

    def _on_tab_search_changed(self, tab_id: str):
        if self._debounce_ids[tab_id] is not None:
            self.after_cancel(self._debounce_ids[tab_id])
        self._debounce_ids[tab_id] = self.after(
            150, lambda: self._apply_tab_search(tab_id))

    # duration filter ranges (min_sec, max_sec) — None = unbounded
    _DUR_RANGES: dict[str, tuple[float | None, float | None]] = {
        "All":   (None, None),
        "<30s":  (None, 30),
        ">30s":  (30, None),
        "30s-1m": (30, 60),
        "1-2m":  (60, 120),
        "2-5m":  (120, 300),
        "5m+":   (300, None),
    }

    def _apply_tab_search(self, tab_id: str):
        self._debounce_ids[tab_id] = None
        query = self._search_vars[tab_id].get()

        if tab_id == "library":
            results = self._lib_cache.search(query)
        elif tab_id == "yoursongs":
            results = self._yoursongs_cache.search(query)
        else:
            results = self._fav_db.search(query)

        # apply duration filter
        dur_filter = self._duration_filters.get(tab_id)
        if dur_filter:
            lo, hi = self._DUR_RANGES.get(dur_filter.get(), (None, None))
            if lo is not None or hi is not None:
                filtered = []
                for r in results:
                    d = r[2]  # duration
                    if d is None:
                        continue
                    if lo is not None and d < lo:
                        continue
                    if hi is not None and d >= hi:
                        continue
                    filtered.append(r)
                results = filtered

        self._visible[tab_id] = results
        lb = self._listboxes[tab_id]
        lb.delete(0, "end")
        for display, abs_path, duration in results:
            dur_str = self._fmt_duration(duration)
            lb.insert("end", f"  [{dur_str}]  {display}")

        self._update_tab_status(tab_id, query)

    def _update_tab_status(self, tab_id: str, query: str = ""):
        sl = self._status_labels[tab_id]
        shown = len(self._visible[tab_id])
        if tab_id == "library":
            total = self._lib_cache.count
        elif tab_id == "yoursongs":
            total = self._yoursongs_cache.count
        else:
            total = self._fav_db.count

        dur_f = self._duration_filters.get(tab_id)
        dur_active = dur_f and dur_f.get() != "All"
        if query.strip() or dur_active:
            sl.config(
                text=f"{shown} match{'es' if shown != 1 else ''} (of {total})",
                fg=COL_TEXT_DIM)
        else:
            word = "favourite" if tab_id == "favourites" else "song"
            sl.config(
                text=f"{total} {word}{'s' if total != 1 else ''}",
                fg=COL_TEXT_DIM)

    @staticmethod
    def _fmt_duration(sec: float | None) -> str:
        if sec is None:
            return "?"
        s = max(0, int(sec))
        return f"{s // 60}:{s % 60:02d}"

    # ══════════════════════════════════════════════════════════
    #  Favourites
    # ══════════════════════════════════════════════════════════

    def _toggle_favourite(self, tab_id: str):
        lb = self._listboxes[tab_id]
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        vis = self._visible[tab_id]
        if idx >= len(vis):
            return
        display, abs_path, duration = vis[idx]
        source = tab_id
        if self._fav_db.is_favourite(abs_path):
            self._fav_db.remove(abs_path)
        else:
            self._fav_db.add(abs_path, source, display, duration)
        self._refresh_fav_list()

    def _remove_favourite_selected(self):
        lb = self._listboxes["favourites"]
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        vis = self._visible["favourites"]
        if idx >= len(vis):
            return
        _, abs_path, _ = vis[idx]
        self._fav_db.remove(abs_path)
        self._refresh_fav_list()

    def _refresh_fav_list(self):
        self._apply_tab_search("favourites")

    # ══════════════════════════════════════════════════════════
    #  Transport controls
    # ══════════════════════════════════════════════════════════

    def _sel_index(self, tab_id: str | None = None):
        if tab_id is None:
            tab_id = self._active_tab
        s = self._listboxes[tab_id].curselection()
        return s[0] if s else None

    def _play_pause(self):
        if self.engine.playing:
            self.engine.toggle_pause()
            self._update_play_button()
            return
        if len(self.queue) == 0:
            self.lbl_now.config(text="Queue is empty \u2014 add songs first",
                                fg=COL_WARN)
            return
        self._play_queue_from_start()

    def _play_queue_from_start(self):
        entry = self.queue.start()
        if entry:
            self._play_entry(entry)

    def _play_entry(self, entry):
        display, abs_path = entry
        song, err = load_song(abs_path)
        if song is None:
            self.lbl_now.config(text=f"Error: {err}", fg=COL_ERR)
            self.queue.pop_current()
            self._refresh_queue_list()
            cur = self.queue.current
            if cur:
                self._play_entry(cur)
            else:
                self.queue.reset()
                self.lbl_now.config(text="Queue finished (errors)",
                                    fg=COL_WARN)
                self._update_play_button()
            return

        # Highlight in active tab if visible
        tab = self._active_tab
        for i, (_, p, _dur) in enumerate(self._visible[tab]):
            if p == abs_path:
                lb = self._listboxes[tab]
                lb.selection_clear(0, "end")
                lb.selection_set(i)
                lb.see(i)
                break

        self._refresh_queue_list()
        self.engine.play(song,
                         on_finish=lambda: self.after(0, self._on_song_done))
        self._update_play_button()

    def _on_song_done(self):
        # Remove the finished song from the queue
        self.queue.pop_current()
        cur = self.queue.current
        if cur:
            self._refresh_queue_list()
            self._play_entry(cur)
        else:
            self.queue.reset()
            self._refresh_queue_list()
            self.lbl_now.config(text="Queue finished", fg=COL_SUCCESS)
            self._update_play_button()
            self.progress.config(to=max(self.engine.total_time, 0.01))
            self.progress.set(self.engine.total_time)

    def _next_song(self):
        if not self.queue.is_active:
            return
        self.engine.stop()
        self.queue.pop_current()
        cur = self.queue.current
        if cur:
            self._play_entry(cur)
        else:
            self._stop()

    def _prev_song(self):
        if not self.queue.is_active:
            return
        # "Prev" restarts the current song
        self.engine.seek(0.0)

    def _stop(self):
        self.engine.stop()
        self.queue.reset()
        self._refresh_queue_list()
        self._update_play_button()
        self.lbl_now.config(text="Stopped", fg=COL_TEXT)
        self.progress.set(0)
        self.progress.config(to=1)
        self.lbl_time_left.config(text="0:00")
        self.lbl_time_right.config(text="0:00")

    def _update_play_button(self):
        if self.engine.playing and not self.engine.paused:
            self.btn_play.config(text="  Pause  ")
        else:
            self.btn_play.config(text="  Play  ")

    # ── cursor navigation (operates on active tab) ───────────

    def _cursor_up(self):
        tab = self._active_tab
        vis = self._visible[tab]
        lb = self._listboxes[tab]
        if not vis:
            return
        sel = lb.curselection()
        idx = sel[0] if sel else len(vis)
        nxt = (idx - 1) % len(vis)
        lb.selection_clear(0, "end")
        lb.selection_set(nxt)
        lb.see(nxt)
        lb.activate(nxt)

    def _cursor_down(self):
        tab = self._active_tab
        vis = self._visible[tab]
        lb = self._listboxes[tab]
        if not vis:
            return
        sel = lb.curselection()
        idx = sel[0] if sel else -1
        nxt = (idx + 1) % len(vis)
        lb.selection_clear(0, "end")
        lb.selection_set(nxt)
        lb.see(nxt)
        lb.activate(nxt)

    # ── queue management ─────────────────────────────────────

    def _add_to_queue(self, tab_id: str | None = None):
        if tab_id is None:
            tab_id = self._active_tab
        lb = self._listboxes[tab_id]
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        vis = self._visible[tab_id]
        if idx >= len(vis):
            return
        display, abs_path, _dur = vis[idx]
        self.queue.add(display, abs_path)
        self._refresh_queue_list()

    def _remove_from_queue(self):
        sel = self.queue_list.curselection()
        if not sel:
            return
        removed_idx = sel[0]
        was_current = (removed_idx == self.queue.current_index)
        self.queue.remove(removed_idx)
        self._refresh_queue_list()
        if was_current and self.engine.playing:
            cur = self.queue.current
            if cur:
                self.engine.stop()
                self._play_entry(cur)
            else:
                self._stop()

    def _clear_queue(self):
        was_playing = self.engine.playing
        self.queue.clear()
        self._refresh_queue_list()
        if was_playing:
            self._stop()

    def _queue_move_up(self):
        sel = self.queue_list.curselection()
        if not sel or sel[0] == 0:
            return
        i = sel[0]
        self.queue.move_up(i)
        self._refresh_queue_list()
        self.queue_list.selection_set(i - 1)

    def _queue_move_down(self):
        sel = self.queue_list.curselection()
        if not sel or sel[0] >= len(self.queue) - 1:
            return
        i = sel[0]
        self.queue.move_down(i)
        self._refresh_queue_list()
        self.queue_list.selection_set(i + 1)

    def _refresh_queue_list(self):
        self.queue_list.delete(0, "end")
        cur_idx = self.queue.current_index
        for i, (display, _) in enumerate(self.queue.items):
            if i == cur_idx and self.queue.is_active:
                self.queue_list.insert("end", f"  NOW  {display}")
                self.queue_list.itemconfig(
                    i, bg=COL_NOW_PLAY, fg="#fff",
                    selectbackground=COL_NOW_PLAY)
            else:
                self.queue_list.insert("end", f"  {i + 1}.  {display}")
        count = len(self.queue)
        if count == 0:
            self.lbl_queue.config(text="Empty", fg=COL_TEXT_DIM)
        else:
            upcoming = (max(0, count - (cur_idx + 1))
                        if self.queue.is_active else count)
            self.lbl_queue.config(
                text=(f"{count} song{'s' if count != 1 else ''}"
                      + (f"  ({upcoming} upcoming)"
                         if self.queue.is_active else "")),
                fg=COL_TEXT)

    # ── scrubber ─────────────────────────────────────────────

    def _seek_start(self, _event):
        self._seeking = True

    def _seek_end(self, _event):
        if self.engine.playing or self.engine.paused:
            self.engine.seek(self.progress.get())
        self._seeking = False

    # ══════════════════════════════════════════════════════════
    #  Periodic ticks
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _fmt(sec):
        s = max(0, int(sec))
        return f"{s // 60}:{s % 60:02d}"

    def _tick_progress(self):
        if self.engine.playing or self.engine.paused:
            cur = self.engine.current_time
            tot = self.engine.total_time
            state = "  [paused]" if self.engine.paused else ""
            self.lbl_now.config(
                text=f"{self.engine.song_name}{state}", fg=COL_TEXT)
            if not self._seeking:
                self.progress.config(to=max(tot, 0.01))
                self.progress.set(min(cur, tot))
            self.lbl_time_left.config(text=self._fmt(cur))
            self.lbl_time_right.config(text=self._fmt(tot))
        self.after(150, self._tick_progress)

    def _tick_sky(self):
        sky_open = False
        sky_focused = False
        try:
            for w in pygetwindow.getWindowsWithTitle("Sky"):
                if w.title == "Sky":
                    sky_open = True
                    try:
                        if w.isActive:
                            sky_focused = True
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        self._sky_open = sky_open
        self._sky_focused = sky_focused

        if sky_focused:
            txt, col = "Sky: Open \u00b7 Focused", COL_SUCCESS
        elif sky_open:
            txt, col = "Sky: Open \u00b7 Not focused", COL_WARN
        else:
            txt, col = "Sky: Not detected", COL_TEXT_DIM

        self.lbl_sky.config(text=txt, fg=col)
        self.after(1500, self._tick_sky)

    # ══════════════════════════════════════════════════════════
    #  Hotkey management
    # ══════════════════════════════════════════════════════════

    def _resolve_hotkeys(self):
        self._hotkey_keys.clear()
        for action, s in self.hotkeys.items():
            try:
                self._hotkey_keys[action] = str_to_pynput_key(s)
            except Exception:
                pass

    def _start_capture(self, action):
        self._capturing = action
        if action in self._hotkey_labels:
            self._hotkey_labels[action].config(text="Press a key\u2026",
                                               fg=COL_WARN)

    def _on_global_key(self, key):
        # ── capturing mode ────────────────────────────────────
        if self._capturing:
            action = self._capturing
            self._capturing = None
            s = pynput_key_to_str(key)
            self.hotkeys[action] = s
            save_hotkeys(self.hotkeys)
            self._resolve_hotkeys()
            if action in self._hotkey_labels:
                self.after(0, lambda: self._hotkey_labels[action].config(
                    text=key_display(s), fg=COL_ACCENT_LT))
            return

        # ── only dispatch when Sky or this app is focused ─────
        fg_title = _foreground_window_title()
        if fg_title not in ("Sky", "Sky Music Player"):
            return

        dispatch = {
            "play_pause":   self._play_pause,
            "next_song":    self._next_song,
            "prev_song":    self._prev_song,
            "stop":         self._stop,
            "cursor_up":    self._cursor_up,
            "cursor_down":  self._cursor_down,
            "add_to_queue": lambda: self._add_to_queue(self._active_tab),
        }
        for action, bound in self._hotkey_keys.items():
            if key == bound:
                self.after(0, dispatch.get(action, lambda: None))
                break

    # ══════════════════════════════════════════════════════════
    #  Cleanup
    # ══════════════════════════════════════════════════════════

    def _on_close(self):
        self.engine.stop()
        self._listener.stop()
        self._lib_cache.close()
        self._yoursongs_cache.close()
        self._fav_db.close()
        self._close_hotkey_window()
        self.destroy()


# ──────────────────────────────────────────────────────────────
#  Entry
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = App()
    app.mainloop()
