#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask",
#     "pywebview",
#     "qtpy",
#     "PyQt6",
#     "PyQt6-WebEngine",
# ]
# ///

"""
hearthmonitor — GUI interface for managing hearth and related apps.

Run:
    [uv run] hearthmonitor.py            # native window

Developer:  KarmaHelen
Contact:    <email>
Support:    https://buymeacoffee.com/karmahelen
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# Suppress Flask access logs for high-frequency polling endpoints
class _QuietPollFilter(logging.Filter):
    NOISY_PATHS = {"/api/get_logs", "/api/get_apps"}
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self.NOISY_PATHS)

logging.getLogger("werkzeug").addFilter(_QuietPollFilter())

BASE_DIR = Path(__file__).resolve().parent
HEARTH_ROOT = BASE_DIR.parent
HEARTH_CONFIG_PATH = HEARTH_ROOT / "hearth.json"

# Linux desktop integration paths. APPS_DIR is the freedesktop.org standard
# location for per-user application launchers (start-menu entries); the
# desktop environment scans it and adds matching .desktop files to its
# launcher. DESKTOP_DIR is where files appear as desktop icons. Both can
# be overridden by tests via direct module attribute assignment.
APPS_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications"
DESKTOP_DIR = Path.home() / "Desktop"

# freedesktop.org main categories for application launchers. Apps can have
# multiple categories, but for our purposes one is sufficient — these are
# the categories the user picks from in the Launchers panel dropdown.
LAUNCHER_CATEGORIES = [
    "AudioVideo", "Audio", "Video", "Development", "Education",
    "Game", "Graphics", "Network", "Office", "Science",
    "Settings", "System", "Utility",
]

sys.path.insert(0, str(HEARTH_ROOT))

from hearth import run

MAX_LOG_LINES = 500


class ManagedApp:
    """Tracks a running subprocess and its captured output.

    Lifecycle events are surfaced as `meta` log entries prefixed with
    `[hearth]`. There are four such events:

    - `Started (pid N)`        — synthesized at construction
    - `Stopped`                 — synthesized when stop() terminates cleanly
    - `Force-killed after timeout` — synthesized when stop() escalates to SIGKILL
    - `Process exited (code N)` — synthesized when the process dies on its own

    Synthesis is centralized in a dedicated watcher thread (`_watch_process`)
    rather than spread across the reader threads. The watcher is the single
    point that decides which message to emit, and waits for the reader threads
    to drain final output before doing so — guaranteeing the meta line appears
    after the app's last actual output line in the panel.

    The watcher distinguishes user-initiated stops from unexpected exits via
    `_stop_initiated`, which stop() sets eagerly (before terminate()) so the
    flag is observable by the watcher even if it wakes up first."""

    def __init__(self, name, process):
        self.name = name
        self.process = process
        self.port = None
        self.logs = deque(maxlen=MAX_LOG_LINES)
        self.lock = threading.Lock()
        self.log_id = 0  # monotonic counter for polling
        self._port_re = re.compile(r"Running on \S+:(\d+)")
        self._ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        # Lifecycle flags read by the watcher to choose the right meta message
        self._stop_initiated = False
        self._stop_force_killed = False
        self._reader_threads = []
        self._watcher_thread = None
        # Emit the startup line first so it appears at the top of the log
        # panel before any of the app's own output arrives.
        self._add_meta(f"Started (pid {process.pid})")
        self._start_readers()
        self._start_watcher()

    def _add_meta(self, text):
        """Append a meta log entry. Used for lifecycle events the monitor
        synthesizes on its own behalf — distinct from app-produced output.
        Lines are prefixed with `[hearth]` so they're textually identifiable
        even when colors aren't preserved (e.g., copied to a text file)."""
        with self.lock:
            self.log_id += 1
            self.logs.append({
                "id": self.log_id,
                "stream": "meta",
                "text": f"[hearth] {text}",
                "ts": time.time(),
            })

    def _start_readers(self):
        for stream, label in [(self.process.stdout, "out"), (self.process.stderr, "err")]:
            t = threading.Thread(target=self._read_stream, args=(stream, label), daemon=True)
            t.start()
            self._reader_threads.append(t)

    def _start_watcher(self):
        self._watcher_thread = threading.Thread(target=self._watch_process, daemon=True)
        self._watcher_thread.start()

    def wait_for_watcher(self, timeout=2.0):
        """Block until the watcher thread has finished synthesizing the exit
        meta line, or until `timeout` seconds elapse. Used by stop_app to
        guarantee the [hearth] Stopped (or Force-killed) line is in the deque
        before stop_app returns to the frontend — without this, there's a
        small window where the next poll could fetch logs and miss the line.

        The timeout is defensive; in practice the watcher does only a brief
        reader-thread join (≤0.5s) and a single deque append after the
        process has already exited."""
        if self._watcher_thread:
            self._watcher_thread.join(timeout=timeout)

    def _watch_process(self):
        """Wait for the process to exit, drain reader threads, then synthesize
        the appropriate exit meta line. Runs once per ManagedApp lifetime."""
        # Block until the process exits (regardless of how — clean exit, our
        # SIGTERM, our SIGKILL, or anything else).
        self.process.wait()
        # Give reader threads a moment to finish draining whatever remained
        # in the OS pipe buffers when the process died. Without this, the
        # exit meta line could appear in the panel before the dying app's
        # final output, which would read confusingly. The 0.5s timeout is
        # generous; in practice readers exit within milliseconds of EOF.
        for t in self._reader_threads:
            t.join(timeout=0.5)
        # Decide which meta line to emit. The flags are set by stop() before
        # it touches the process, so checking them here is race-free even if
        # this watcher woke up at the same instant stop() did.
        if self._stop_initiated:
            if self._stop_force_killed:
                self._add_meta("Force-killed after timeout")
            else:
                self._add_meta("Stopped")
        else:
            self._add_meta(f"Process exited (code {self.process.returncode})")

    def _read_stream(self, stream, label):
        try:
            for raw_line in stream:
                line = self._ansi_re.sub("", raw_line.rstrip("\n"))
                # Detect port from Flask's "Running on http://0.0.0.0:XXXX"
                if self.port is None:
                    m = self._port_re.search(line)
                    if m:
                        self.port = int(m.group(1))
                with self.lock:
                    self.log_id += 1
                    self.logs.append({
                        "id": self.log_id,
                        "stream": label,
                        "text": line,
                        "ts": time.time(),
                    })
        except Exception:
            pass

    def get_logs_since(self, since_id):
        with self.lock:
            return [entry for entry in self.logs if entry["id"] > since_id]

    def is_running(self):
        return self.process.poll() is None

    def stop(self):
        if self.is_running():
            # Set the flag BEFORE touching the process so the watcher thread
            # can never observe an exit with the flag still unset (which
            # would cause it to mis-classify a user stop as an unexpected
            # exit). The watcher uses this flag, not stop() itself, to
            # synthesize the meta line — keeping all lifecycle messaging
            # centralized in one place.
            self._stop_initiated = True
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop_force_killed = True
                self.process.kill()
                self.process.wait()
        self._close_pipes()

    def _close_pipes(self):
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except Exception:
                pass


class HearthMonitor:
    def __init__(self):
        self.managed = {}  # name -> ManagedApp
        self._lock = threading.Lock()
        self._config_path = BASE_DIR / "hearthmonitor.json"
        self._ports = self._load_ports()

    def _load_monitor_config(self):
        """Read hearthmonitor.json. Returns dict (empty if missing/unreadable).
        Holds monitor-specific settings: per-app port assignments, the
        Run-using-uv toggle, etc. Distinct from hearth.json, which holds
        framework-level config (aliases, password hash, tray/terminal flags).
        The split keeps monitor concerns out of the file the framework reads."""
        try:
            return json.loads(self._config_path.read_text())
        except Exception:
            return {}

    def _save_monitor_config(self, data):
        """Write hearthmonitor.json. Best-effort — silent on failure, same as
        the prior _save_ports behavior. Callers always pass a complete dict
        so partial fields aren't dropped on save."""
        try:
            self._config_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_ports(self):
        return self._load_monitor_config().get("ports", {})

    def _save_ports(self):
        # Read-modify-write so other monitor settings (use_uv, etc.) survive
        # a ports update. Previously this method wrote {"ports": ...} as the
        # entire file body, which would have clobbered any other top-level
        # field when added.
        config = self._load_monitor_config()
        config["ports"] = self._ports
        self._save_monitor_config(config)

    def _get_use_uv(self):
        """Read the Run-using-uv preference. Defaults to True if absent.
        When True (the default), launches go through uv; when False, scripts
        are invoked directly via their shebang and the user is responsible
        for Python and package management. Lives in hearthmonitor.json since
        it's a monitor-specific concern — the framework (hearth.py) doesn't
        care how the script got invoked, only that it's now running."""
        config = self._load_monitor_config()
        return bool(config.get("use_uv", True))

    def set_uv_mode(self, enabled):
        """Toggle Run-using-uv on or off. Writes hearthmonitor.json, then
        refreshes every existing .desktop launcher so the Exec= line matches
        the new mode. Returns {"use_uv": bool, "refreshed": [names], "failures": [...]}.

        The refresh is best-effort per-app: if one app's launcher rewrite
        fails (uv missing while toggling INTO uv mode, file system issue,
        etc.), other apps' refreshes still proceed and the failure is
        reported in the failures list."""
        want = bool(enabled)
        config = self._load_monitor_config()
        config["use_uv"] = want
        self._save_monitor_config(config)

        # Refresh every existing launcher to match the new mode. Discover
        # apps with launchers by checking both directories (the same way
        # get_launchers does).
        refreshed = []
        failures = []
        for name in self._discover_apps() + [self._MONITOR_NAME]:
            sm_file, dt_file = self._launcher_paths(name)
            if not (sm_file.exists() or dt_file.exists()):
                continue  # No launcher to refresh
            err = self._refresh_launchers_for_app(name)
            if err:
                failures.append({"name": name, "error": err})
            else:
                refreshed.append(name)

        return {"use_uv": want, "refreshed": refreshed, "failures": failures}

    def _load_hearth_config(self):
        """Read the central hearth.json. Returns dict (empty if missing/unreadable)."""
        try:
            return json.loads(HEARTH_CONFIG_PATH.read_text())
        except Exception:
            return {}

    def _save_hearth_config(self, data):
        """Write hearth.json atomically (temp file + rename) to avoid partial reads
        from the framework if it loads config concurrently."""
        tmp = HEARTH_CONFIG_PATH.with_suffix(HEARTH_CONFIG_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(HEARTH_CONFIG_PATH)

    @staticmethod
    def _resolve_alias(name, hearth_config):
        """Return the alias for `name`, or None if not set/blank/equal to anchor.
        Mirrors the rule used by hearth.py's _get_app_alias so framework and
        monitor agree on what counts as 'no alias'."""
        raw = (hearth_config.get("apps", {}).get(name, {}) or {}).get("alias")
        if not raw:
            return None
        cleaned = raw.strip()
        if not cleaned or cleaned == name:
            return None
        return cleaned

    def _discover_apps(self):
        """Scan sibling directories for Hearth apps."""
        apps = []
        for d in sorted(HEARTH_ROOT.iterdir()):
            if not d.is_dir():
                continue
            script = d / f"{d.name}.py"
            if not script.exists():
                continue
            # Quick check: does it import from hearth?
            try:
                text = script.read_text(encoding="utf-8", errors="ignore")
                if "from hearth import" not in text:
                    continue
            except Exception:
                continue
            apps.append(d.name)
        return apps

    def get_apps(self):
        """Return list of discovered apps with their status."""
        discovered = self._discover_apps()
        hearth_config = self._load_hearth_config()
        result = []
        with self._lock:
            for name in discovered:
                if name == "hearthmonitor":
                    continue
                managed = self.managed.get(name)
                if managed and managed.is_running():
                    status = "running"
                elif managed:
                    # Process exited but we keep the entry so logs survive
                    status = "exited"
                else:
                    status = "stopped"

                alias = self._resolve_alias(name, hearth_config)
                display_name = alias if alias else name

                result.append({
                    "name": name,                  # anchor — identity, used for all dispatch
                    "display_name": display_name,  # what the user reads
                    "alias": alias,                # raw alias (or None) — drives the edit UI
                    "status": status,
                    "port": managed.port if managed else None,
                    "saved_port": self._ports.get(name),
                })
        # Sort by what the user sees, case-insensitively. Anchor is the tiebreaker
        # so order is deterministic when two apps happen to share a display name.
        result.sort(key=lambda a: (a["display_name"].lower(), a["name"]))
        return result

    def _refresh_launchers_for_app(self, name):
        """Rewrite any existing .desktop files for `name` to reflect current
        config (alias, etc.). No-op if no launchers exist for the app — we
        don't create launchers as a side-effect of other operations.

        Returns None on success or no-op. Returns an error message string
        on failure; caller is responsible for surfacing it. Failures are
        non-fatal at the alias-write level: alias persistence is the source
        of truth, and a launcher rewrite failure means a derived artifact
        is briefly stale, which the user can fix from the Launchers panel.
        """
        sm_file, dt_file = self._launcher_paths(name)
        if not sm_file.exists() and not dt_file.exists():
            return None  # no launchers → nothing to refresh

        # Read what's on disk so we preserve category — it's a per-launcher
        # property that lives only in the .desktop file. (Terminal used to
        # come from here too, but it now lives in hearth.json as a per-app
        # preference; see set_terminal_flags for the full story.)
        parsed = {}
        for f in (sm_file, dt_file):
            if f.exists():
                parsed = self._parse_desktop_file(f)
                break

        # uv path is only needed when generating uv-mode launcher lines.
        # In no-uv mode, _build_desktop_content doesn't use uv_path at all,
        # so missing uv isn't a blocker for launcher refresh.
        if self._get_use_uv():
            uv_path = self._resolve_uv_path()
            if not uv_path:
                return "uv not found in PATH; cannot rewrite launcher"
        else:
            uv_path = None

        intent = {
            "start_menu": sm_file.exists(),
            "desktop": dt_file.exists(),
            "category": parsed.get("category") if parsed.get("category") in LAUNCHER_CATEGORIES else "Utility",
            # terminal omitted — _build_desktop_content reads it from
            # hearth.json, not from this intent dict.
        }
        try:
            self._reconcile_launcher(name, intent, uv_path, self._load_hearth_config())
        except Exception as e:
            return str(e)
        return None

    def set_alias(self, name, alias):
        """Set or clear the user-facing alias for an app. Empty/whitespace-only
        alias, or alias equal to the anchor name, removes the entry entirely.
        Writes to hearth.json (the architectural brain) — the framework will
        pick up the new value on the next app launch."""
        if not isinstance(name, str) or not name:
            return {"error": "Invalid app name"}

        # Validate the app actually exists on disk before writing config for it,
        # so we don't accumulate aliases for apps that aren't there.
        discovered = self._discover_apps()
        if name not in discovered or name == "hearthmonitor":
            return {"error": f"Unknown app: {name}"}

        cleaned = (alias or "").strip()
        config = self._load_hearth_config()
        apps = config.setdefault("apps", {})
        entry = apps.get(name) or {}

        if not cleaned or cleaned == name:
            # Clear the alias. Drop the whole entry if nothing else is in it,
            # so the config file stays clean.
            if "alias" in entry:
                del entry["alias"]
            if entry:
                apps[name] = entry
            else:
                apps.pop(name, None)
            # If apps section is now empty, drop it too.
            if not apps:
                config.pop("apps", None)
            self._save_hearth_config(config)
            # Refresh any existing launcher files so the Name= field tracks
            # the cleared alias (back to the anchor). Non-fatal failure —
            # the alias write is the source of truth; a stale launcher can
            # be fixed by re-applying from the Launchers panel.
            err = self._refresh_launchers_for_app(name)
            if err:
                print(f"[Hearth] Alias for {name} updated. Could not refresh "
                      f"launchers: {err}. Re-apply from the Launchers panel "
                      f"to retry.", file=sys.stderr)
            return {"name": name, "alias": None}

        entry["alias"] = cleaned
        apps[name] = entry
        config["apps"] = apps
        self._save_hearth_config(config)
        # Refresh any existing launcher files so the Name= field reflects
        # the new alias. Non-fatal failure — see comment above.
        err = self._refresh_launchers_for_app(name)
        if err:
            print(f"[Hearth] Alias for {name} updated. Could not refresh "
                  f"launchers: {err}. Re-apply from the Launchers panel "
                  f"to retry.", file=sys.stderr)
        return {"name": name, "alias": cleaned}

    def get_password(self):
        """Return the current Hearth auth password. Empty string means auth is
        disabled (the framework treats missing key and empty string identically
        as 'no auth')."""
        config = self._load_hearth_config()
        return {"password": config.get("password") or ""}

    def set_password(self, password):
        """Set or clear the Hearth auth password in the central config.

        Empty/whitespace-only value removes the `password` key entirely,
        disabling authentication for serve mode on next app launch. A non-empty
        value is stored verbatim (no trimming) — passwords with leading or
        trailing spaces are preserved as the user typed them, since that's a
        legitimate credential choice. The empty/whitespace check is only for
        deciding 'is this a clear-the-password gesture'."""
        if password is None:
            password = ""
        # Detect "clear" intent without mutating the value we'd store
        is_clear = (password.strip() == "")

        config = self._load_hearth_config()
        if is_clear:
            config.pop("password", None)
        else:
            config["password"] = password
        self._save_hearth_config(config)
        return {"password": password if not is_clear else "",
                "auth_enabled": not is_clear}

    # ---- Launchers (.desktop file management) -----------------------------
    #
    # Hearth Monitor can create and remove freedesktop.org-style .desktop
    # files in two locations: ~/.local/share/applications/ (start-menu) and
    # ~/Desktop/ (desktop icons). The filename uses the anchor name so it's
    # stable across alias renames; the Name= field inside uses the alias if
    # set (so the user-facing label tracks renames). The Exec= line invokes
    # `uv run` with the absolute path to uv (resolved via shutil.which) so
    # the launcher works regardless of $PATH inheritance.

    @staticmethod
    def _resolve_uv_path():
        """Return the absolute path to the uv binary, or None if not found."""
        return shutil.which("uv")

    @staticmethod
    def _is_uv_managed_venv(venv_path):
        """Detect whether a venv at venv_path was created by uv.

        uv writes a `uv = <version>` line into the venv's pyvenv.cfg when
        it creates one. stdlib venv, virtualenv, and conda don't write
        this marker. So the presence of that line is a reliable signal
        that the venv is uv-managed.

        Returns False whenever we can't read the file or the marker is
        absent — the safe default is 'don't strip user state we're not
        sure about.'"""
        try:
            with open(os.path.join(venv_path, "pyvenv.cfg")) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("uv = ") or stripped.startswith("uv="):
                        return True
            return False
        except (OSError, FileNotFoundError):
            return False

    @classmethod
    def _clean_env_for_no_uv(cls):
        """Return a copy of os.environ with uv's runtime contributions
        removed, suitable for spawning a subprocess in no-uv mode.

        Two layers of cleaning, each scoped to avoid affecting unrelated
        user state:

        1. VIRTUAL_ENV / PATH cleaning is conditional. We only strip these
           when the current VIRTUAL_ENV points at a uv-managed venv (detected
           via pyvenv.cfg). If the user has activated their own venv (stdlib
           venv, virtualenv, conda, etc.), VIRTUAL_ENV is left alone and the
           venv's bin stays in PATH — the user's intentional environment
           setup is preserved.

        2. UV_* variables are stripped unconditionally. These are
           definitionally uv's namespace, and they're meaningless to a
           subprocess that isn't running uv. Removing them keeps the
           child's environment predictable without affecting non-uv state.

        Everything else (HOME, USER, LANG, LC_*, DISPLAY, XDG_*, custom
        user env, the rest of PATH) passes through untouched."""
        env = os.environ.copy()
        venv = env.get("VIRTUAL_ENV")
        if venv and cls._is_uv_managed_venv(venv):
            env.pop("VIRTUAL_ENV", None)
            env.pop("VIRTUAL_ENV_PROMPT", None)
            venv_bin = os.path.join(venv, "bin")
            path = env.get("PATH", "").split(os.pathsep)
            env["PATH"] = os.pathsep.join(p for p in path if p != venv_bin)
        for k in list(env):
            if k.startswith("UV_"):
                del env[k]
        return env

    def _parse_desktop_file(self, path):
        """Read an existing .desktop file and extract just the fields the UI
        cares about (category, terminal). Tolerant of unknown fields and
        malformed lines — returns whatever was parsable."""
        result = {}
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return result
        for line in text.splitlines():
            if line.startswith("Categories="):
                cats = line[len("Categories="):].rstrip(";").split(";")
                cats = [c.strip() for c in cats if c.strip()]
                if cats:
                    result["category"] = cats[0]
            elif line.startswith("Terminal="):
                result["terminal"] = line[len("Terminal="):].strip().lower() == "true"
        return result

    def _build_desktop_content(self, name, intent, uv_path, hearth_config):
        """Construct the .desktop file body for a given app and intent.

        - `Name` uses the app's display name (alias-or-anchor) so renames
          flow into the start-menu and desktop labels.
        - `Exec` shape depends on the Run-using-uv setting in
          hearthmonitor.json. When uv mode is on (the default): the line is
          `<uv_path> run <script>` and uses the absolute uv path (no PATH
          reliance, since desktop environments don't inherit a user's shell
          PATH cleanly). When uv mode is off: the line is just `<script>`,
          relying on the script's shebang for invocation; the user is
          responsible for the script being executable and for managing
          Python and packages themselves.
        - `Path` sets cwd to the app's directory, so BASE_DIR-relative file
          paths inside the app continue to work.
        - `Icon` uses the app's <name>.ico if it exists, else falls back to
          the standard `python3` themed icon.
        - `Categories` uses the user-selected category from the intent dict,
          defaulting to Utility if missing or unrecognized.
        - `Terminal` is read from `hearth.json` (apps.<name>.terminal), NOT
          from the intent dict. Terminal is a per-app preference that
          applies to all launches of the app — both launcher .desktop files
          and the OPEN button — so it lives in the central config rather
          than being recomputed per-launcher.

        Note: uv_path may be None in no-uv mode; in that case it's simply
        unused, and callers don't need to resolve uv at all when generating
        a no-uv launcher."""
        alias = self._resolve_alias(name, hearth_config)
        display = alias if alias else name

        script = HEARTH_ROOT / name / f"{name}.py"
        icon_file = HEARTH_ROOT / name / f"{name}.ico"
        icon_value = str(icon_file) if icon_file.exists() else "python3"

        category = (intent.get("category") or "Utility").strip()
        if category not in LAUNCHER_CATEGORIES:
            category = "Utility"

        # Terminal flag from hearth.json — same source the OPEN button reads
        # from, so the flag's effect is consistent across launch paths.
        terminal_enabled = bool((hearth_config.get("apps", {}).get(name, {}) or {}).get("terminal", False))
        terminal = "true" if terminal_enabled else "false"

        # Choose Exec= shape based on the Run-using-uv preference.
        if self._get_use_uv():
            exec_line = f"{uv_path} run {script}"
        else:
            exec_line = str(script)

        return (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={display}\n"
            f"Exec={exec_line}\n"
            f"Path={HEARTH_ROOT / name}\n"
            f"Terminal={terminal}\n"
            f"Icon={icon_value}\n"
            f"Categories={category};\n"
        )

    def _launcher_paths(self, name):
        """Return (apps_dir_file, desktop_file) for an app's launchers. The
        APPS_DIR and DESKTOP_DIR module globals are resolved at call time,
        so tests overriding them directly on the module take effect without
        any plumbing here."""
        return APPS_DIR / f"{name}.desktop", DESKTOP_DIR / f"{name}.desktop"

    def get_launchers(self):
        """Return launcher state per discovered app, plus environment info.

        Each entry includes the current state of both possible launcher files
        (start_menu, desktop) and — when at least one exists — the category
        and terminal flag parsed from it. The start-menu file is treated as
        authoritative when both exist and disagree; Apply will rewrite both
        identically, healing any drift."""
        discovered = self._discover_apps()
        hearth_config = self._load_hearth_config()
        result = []
        with self._lock:
            for name in discovered:
                if name == "hearthmonitor":
                    continue
                start_menu_file, desktop_file = self._launcher_paths(name)
                start_menu_exists = start_menu_file.exists()
                desktop_exists = desktop_file.exists()
                # Read whichever file exists; prefer start-menu as authoritative
                parsed = {}
                for f in (start_menu_file, desktop_file):
                    if f.exists():
                        parsed = self._parse_desktop_file(f)
                        break
                result.append({
                    "name": name,
                    # Include the alias when set (None otherwise), so the
                    # frontend can display the user-facing name as primary
                    # and use the anchor as a hover-only tooltip when an
                    # alias is in effect. Same display pattern as the sidebar.
                    "alias": self._resolve_alias(name, hearth_config),
                    "start_menu": start_menu_exists,
                    "desktop": desktop_exists,
                    "category": parsed.get("category") if parsed.get("category") in LAUNCHER_CATEGORIES else "Utility",
                    # Terminal flag now lives in hearth.json (apps.<name>.terminal)
                    # rather than being parsed from the .desktop file. This way
                    # it's a per-app preference that applies to all launches
                    # (launcher AND the OPEN button), not just to the launcher.
                    "terminal": bool((hearth_config.get("apps", {}).get(name, {}) or {}).get("terminal", False)),
                    # Tray flag from hearth.json — read alongside the launcher
                    # state because both are per-app desktop-integration knobs
                    # the user edits in the same panel, even though they're
                    # written by different backend methods (.desktop files vs.
                    # hearth.json apps.<name>.tray).
                    "tray": bool((hearth_config.get("apps", {}).get(name, {}) or {}).get("tray", False)),
                })
        # Sort by display name (alias-or-anchor) so the panel's reading order
        # matches what the user sees in the sidebar and aliases panel. Anchor
        # is the secondary key so apps without an alias still order
        # alphabetically among themselves.
        result.sort(key=lambda a: ((a["alias"] or a["name"]).lower(), a["name"]))
        return {
            "uv_path": self._resolve_uv_path(),
            "use_uv": self._get_use_uv(),
            "categories": list(LAUNCHER_CATEGORIES),
            "launchers": result,
        }

    def set_launchers(self, changes):
        """Reconcile the on-disk launcher state with the user's intent.

        `changes` is a list of per-app intent dicts:
            {name, start_menu (bool), desktop (bool), category (str), terminal (bool)}
        For each app, the on-disk files are created, updated, or deleted to
        match. When neither location is requested, both files are removed —
        intentional, since the user cleared both checkboxes to mean 'no
        launcher.' Stale files are exactly what we don't want sitting around.

        Failures are collected per-app and returned, rather than aborting
        the whole batch — same partial-failure model as set_alias's panel
        path. Successful changes are persisted; the user can fix and retry."""
        # uv only needed in uv mode. In no-uv mode, Exec= is just the
        # script path, so we can create launchers without uv being present.
        if self._get_use_uv():
            uv_path = self._resolve_uv_path()
            if not uv_path:
                return {"error": "uv not found in PATH; cannot create launchers"}
        else:
            uv_path = None

        # Make sure target directories exist before any write attempts. Both
        # are user-owned and missing only on freshly-created accounts, but
        # mkdir is idempotent so no harm in always doing it.
        APPS_DIR.mkdir(parents=True, exist_ok=True)
        DESKTOP_DIR.mkdir(parents=True, exist_ok=True)

        discovered = set(self._discover_apps())
        hearth_config = self._load_hearth_config()
        failures = []
        applied = []

        for change in changes or []:
            name = change.get("name")
            # Permit hearthmonitor explicitly — it's filtered out of
            # get_launchers (so it doesn't appear in the operated-apps panel)
            # but is configurable through its own settings modal, which
            # writes here with name="hearthmonitor".
            valid = name and (name in discovered or name == "hearthmonitor")
            if not valid:
                failures.append({"name": name or "(unknown)", "error": "Unknown app"})
                continue
            try:
                self._reconcile_launcher(name, change, uv_path, hearth_config)
                applied.append(name)
            except Exception as e:
                failures.append({"name": name, "error": str(e)})

        return {"applied": applied, "failures": failures}

    def _reconcile_launcher(self, name, intent, uv_path, hearth_config):
        """Create/update/delete the two launcher files for one app to match
        intent. Raises on filesystem errors — caller handles per-app failure
        collection."""
        start_menu_file, desktop_file = self._launcher_paths(name)
        want_start = bool(intent.get("start_menu"))
        want_desktop = bool(intent.get("desktop"))

        content = None
        if want_start or want_desktop:
            content = self._build_desktop_content(name, intent, uv_path, hearth_config)

        for path, want in [(start_menu_file, want_start), (desktop_file, want_desktop)]:
            if want:
                path.write_text(content, encoding="utf-8")
                # 0o755 — Cinnamon and other desktop environments require the
                # exec bit to trust .desktop files (refusing to launch them
                # otherwise as a security measure).
                path.chmod(0o755)
            elif path.exists():
                path.unlink()

    def set_tray_flags(self, changes):
        """Reconcile per-app tray preferences in hearth.json.

        Sibling method to set_launchers — kept separate because the two
        write to different files (hearth.json vs. .desktop files) and have
        different validity rules (tray writes don't require uv to be
        installed; launcher writes do). The Launchers panel calls both on
        Apply when both kinds of change are pending.

        `changes` is a list of {name, tray} dicts. For each, sets or removes
        the apps.<name>.tray field in hearth.json based on the boolean.
        Falsy values delete the key entirely (mirroring the alias-clear
        pattern, so the on-disk shape stays clean).

        Failures are collected per-app rather than aborting the batch."""
        discovered = set(self._discover_apps())
        failures = []
        applied = []

        # Read once, mutate in memory, write once. Atomic-write pattern in
        # _save_hearth_config means partial writes are impossible.
        config = self._load_hearth_config()
        apps = config.setdefault("apps", {})
        modified = False

        for change in changes or []:
            name = change.get("name")
            # Permit hearthmonitor explicitly — it's filtered out of
            # get_launchers so it doesn't appear in the panel, but the
            # cogwheel settings modal also writes here.
            valid = name and (name in discovered or name == self._MONITOR_NAME)
            if not valid:
                failures.append({"name": name or "(unknown)", "error": "Unknown app"})
                continue

            want_tray = bool(change.get("tray"))
            entry = apps.get(name) or {}

            if want_tray:
                if entry.get("tray") is not True:
                    entry["tray"] = True
                    apps[name] = entry
                    modified = True
            else:
                # Drop the tray key. If the entry now has no fields left,
                # drop the entry entirely so the apps section stays clean.
                if "tray" in entry:
                    del entry["tray"]
                    modified = True
                    if entry:
                        apps[name] = entry
                    else:
                        apps.pop(name, None)

            applied.append(name)

        if modified:
            # Drop the apps section if it ended up empty
            if not apps:
                config.pop("apps", None)
            else:
                config["apps"] = apps
            try:
                self._save_hearth_config(config)
            except Exception as e:
                # Whole-batch write failure — every "applied" name actually
                # failed. Report as such rather than misleading the caller.
                return {"applied": [], "failures": [
                    {"name": n, "error": f"hearth.json write failed: {e}"} for n in applied
                ]}

        return {"applied": applied, "failures": failures}

    def set_terminal_flags(self, changes):
        """Reconcile per-app terminal preferences in hearth.json.

        Sibling method to set_tray_flags — same shape, same write contract,
        different field. Writes apps.<name>.terminal in hearth.json. The
        flag is read by two consumers:

          1. Launcher .desktop file generation — sets the Terminal=true/false
             line, controlling whether the desktop environment wraps the
             launcher's Exec= in a terminal emulator.
          2. The OPEN button — when true, wraps the spawned uv command in
             a terminal emulator (x-terminal-emulator → gnome-terminal →
             xterm fallback chain) so the user sees stdout/stderr live.

        Both consumers read from the same field, so the user gets consistent
        behavior whether they launch via the start menu or via OPEN.

        `changes` is a list of {name, terminal} dicts. Same partial-success
        and idempotent behavior as set_tray_flags."""
        discovered = set(self._discover_apps())
        failures = []
        applied = []

        config = self._load_hearth_config()
        apps = config.setdefault("apps", {})
        modified = False

        for change in changes or []:
            name = change.get("name")
            valid = name and (name in discovered or name == self._MONITOR_NAME)
            if not valid:
                failures.append({"name": name or "(unknown)", "error": "Unknown app"})
                continue

            want_terminal = bool(change.get("terminal"))
            entry = apps.get(name) or {}

            if want_terminal:
                if entry.get("terminal") is not True:
                    entry["terminal"] = True
                    apps[name] = entry
                    modified = True
            else:
                if "terminal" in entry:
                    del entry["terminal"]
                    modified = True
                    if entry:
                        apps[name] = entry
                    else:
                        apps.pop(name, None)

            applied.append(name)

        if modified:
            if not apps:
                config.pop("apps", None)
            else:
                config["apps"] = apps
            try:
                self._save_hearth_config(config)
            except Exception as e:
                return {"applied": [], "failures": [
                    {"name": n, "error": f"hearth.json write failed: {e}"} for n in applied
                ]}

        # Terminal changes affect launcher .desktop files too — when the user
        # toggles Terminal in the panel, any existing launchers should be
        # rewritten to reflect the new flag. Refresh after persisting; same
        # non-fatal-failure pattern as alias-driven launcher refresh (the
        # source-of-truth write succeeded; a derived artifact being briefly
        # stale is recoverable).
        for name in applied:
            err = self._refresh_launchers_for_app(name)
            if err:
                print(f"[Hearth] Terminal flag for {name} updated. Could not "
                      f"refresh launchers: {err}. Re-apply from the Launchers "
                      f"panel to retry.", file=sys.stderr)

        return {"applied": applied, "failures": failures}

    # ---- Hearth Monitor self-settings -------------------------------------
    #
    # The monitor is the operator, not one of the operated apps — it's
    # filtered out of the Aliases and Launchers panels. But the monitor
    # still has settings of its own (its launcher preferences and its tray
    # preference), and they're managed through a dedicated settings modal
    # accessed via the cogwheel button in the topbar. These two methods
    # are the backend for that modal.

    _MONITOR_NAME = "hearthmonitor"

    def get_monitor_settings(self):
        """Return the monitor's own launcher and tray preferences as a
        single bundle for the settings modal. Combines launcher state
        (read from .desktop files on disk) with the tray and terminal
        flags (read from hearth.json — per-app preferences that apply to
        all launches, not just the .desktop launcher)."""
        sm_file, dt_file = self._launcher_paths(self._MONITOR_NAME)
        parsed = {}
        for f in (sm_file, dt_file):
            if f.exists():
                parsed = self._parse_desktop_file(f)
                break

        config = self._load_hearth_config()
        monitor_entry = config.get("apps", {}).get(self._MONITOR_NAME, {}) or {}

        return {
            "uv_path": self._resolve_uv_path(),
            "use_uv": self._get_use_uv(),
            "categories": list(LAUNCHER_CATEGORIES),
            "start_menu": sm_file.exists(),
            "desktop": dt_file.exists(),
            "category": parsed.get("category") if parsed.get("category") in LAUNCHER_CATEGORIES else "Utility",
            "terminal": bool(monitor_entry.get("terminal", False)),
            "tray": bool(monitor_entry.get("tray", False)),
        }

    def set_monitor_settings(self, settings):
        """Apply the monitor's own launcher and per-app preference settings.
        The launcher half goes through the same set_launchers reconcile path
        used for operated apps, just with name='hearthmonitor'. The tray and
        terminal halves go through set_tray_flags and set_terminal_flags,
        writing to hearth.json under apps.hearthmonitor.<flag>.

        Returns a result dict that mirrors set_launchers for the launcher
        half ({applied, failures}), with additional 'tray', 'terminal', and
        'use_uv' fields confirming the persisted values. The halves are
        written independently — failures in one don't prevent the others
        from proceeding, so the user sees exactly what succeeded."""
        settings = settings or {}
        result = {"applied": [], "failures": [], "tray": None, "terminal": None, "use_uv": None}

        # Run-using-uv toggle — write FIRST, before any launcher reconcile,
        # because _build_desktop_content reads the use_uv flag to decide
        # the Exec= line shape. set_uv_mode also refreshes every existing
        # launcher to the new mode; after this, the per-monitor launcher
        # reconcile below sees the correct flag and writes the right shape.
        if "use_uv" in settings:
            uv_result = self.set_uv_mode(bool(settings["use_uv"]))
            result["use_uv"] = uv_result.get("use_uv")
            for f in uv_result.get("failures", []):
                result["failures"].append(f)

        # Terminal half — write before the launcher reconcile, because
        # _build_desktop_content reads the terminal flag from hearth.json
        # when generating the .desktop file. If we wrote launcher first and
        # terminal second, the launcher would temporarily have a stale
        # Terminal= line until the next refresh.
        terminal_result = self.set_terminal_flags([
            {"name": self._MONITOR_NAME, "terminal": bool(settings.get("terminal"))}
        ])
        if terminal_result.get("failures"):
            for f in terminal_result["failures"]:
                result["failures"].append(f)
        if self._MONITOR_NAME in terminal_result.get("applied", []):
            result["terminal"] = bool(settings.get("terminal"))

        # Launcher half. Always go through set_launchers so all the existing
        # validation, atomic-write, and uv-resolution logic runs identically
        # regardless of which surface invoked it. Terminal is intentionally
        # NOT in the launcher_change dict — _build_desktop_content reads it
        # from hearth.json (just written above).
        launcher_change = {
            "name": self._MONITOR_NAME,
            "start_menu": bool(settings.get("start_menu")),
            "desktop": bool(settings.get("desktop")),
            "category": settings.get("category") or "Utility",
        }
        launcher_result = self.set_launchers([launcher_change])
        # set_launchers may return a top-level error when uv is missing —
        # propagate it as a failure entry rather than aborting the whole
        # call, so the tray half can still proceed.
        if "error" in launcher_result:
            result["failures"].append({"name": self._MONITOR_NAME, "error": launcher_result["error"]})
        else:
            result["applied"] = launcher_result.get("applied", [])
            result["failures"].extend(launcher_result.get("failures", []))

        # Tray half — go through set_tray_flags for symmetry with the
        # operated-apps Launchers panel and to keep the hearth.json mutation
        # in one place.
        tray_result = self.set_tray_flags([
            {"name": self._MONITOR_NAME, "tray": bool(settings.get("tray"))}
        ])
        if tray_result.get("failures"):
            for f in tray_result["failures"]:
                result["failures"].append(f)
        if self._MONITOR_NAME in tray_result.get("applied", []):
            result["tray"] = bool(settings.get("tray"))

        return result

    def start_app(self, name, port=None):
        """Start a Hearth app in serve mode."""
        with self._lock:
            # Already running?
            managed = self.managed.get(name)
            if managed and managed.is_running():
                return {"error": f"{name} is already running"}
            # Clear stale entry if process already exited
            if managed:
                managed._close_pipes()
                del self.managed[name]

            script = HEARTH_ROOT / name / f"{name}.py"
            if not script.exists():
                return {"error": f"Script not found: {script}"}

            # Honor the Run-using-uv setting for the serve-mode spawn.
            # In uv mode: `uv run script.py --serve [port]`.
            # In no-uv mode: `script.py --serve [port]`, relying on the
            # script's shebang and +x bit (user-managed).
            use_uv = self._get_use_uv()
            if use_uv:
                cmd = ["uv", "run", str(script), "--serve"]
            else:
                cmd = [str(script), "--serve"]
            if port:
                cmd.append(str(port))

            # In no-uv mode, strip uv's environment contributions so the
            # subprocess doesn't accidentally inherit the monitor's uv-managed
            # venv via PATH. Without this, a monitor running under uv would
            # leak its venv (with all its packages) to spawned scripts —
            # making them appear to "work" even when the user's system Python
            # lacks the required packages, masking real configuration issues.
            spawn_env = None if use_uv else self._clean_env_for_no_uv()

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,  # line-buffered
                    cwd=str(HEARTH_ROOT),
                    env=spawn_env,
                )
            except Exception as e:
                return {"error": str(e)}

            self.managed[name] = ManagedApp(name, proc)

            # Persist port assignment
            if port:
                self._ports[name] = int(port)
                self._save_ports()

            return {"started": name, "pid": proc.pid}

    def stop_app(self, name):
        """Stop a running Hearth app.

        The ManagedApp entry is RETAINED in 'exited' status (not deleted
        from self.managed). This mirrors how self-exited apps are handled —
        their logs persist until the user starts the app again, at which
        point start_app's existing 'Clear stale entry' path cleans up the
        old entry. Same end state, same cleanup, regardless of how the
        process died.

        Without this, two related symptoms appear: (1) the watcher's
        [hearth] Stopped meta line never reaches the frontend because the
        deque is on an orphaned ManagedApp instance, and (2) clicking away
        and back to the stopped app shows an empty log panel because
        get_logs has nothing to look up.

        After stop() returns, we join the watcher thread to guarantee the
        meta line is in the deque before this method returns to the
        frontend — closing the small race where a poll could otherwise
        fetch logs and miss the synthesis."""
        with self._lock:
            managed = self.managed.get(name)
            if not managed or not managed.is_running():
                return {"error": f"{name} is not running"}

            managed.stop()
            managed.wait_for_watcher()
            return {"stopped": name}

    def open_app(self, name):
        """Launch an app in local mode (its own pywebview window).

        Unlike start_app, the monitor doesn't track or manage the spawned
        process — it's a fire-and-forget launcher, equivalent to clicking
        a .desktop icon. The framework's single-instance lockfile (local
        mode only) prevents duplicate launches.

        Returns:
          {"opened": name, "pid": N} on a fresh launch
          {"already_open": True, "name": name} when a local-mode instance
            is already running per the lockfile
          {"error": "..."} for spawn failures or missing prerequisites

        Note: this method does NOT prevent simultaneous serve-mode + local-
        mode runs of the same app. Serve mode doesn't write a lockfile, so
        the local-mode launch can succeed even if a serve-mode process is
        running. That's a corruption risk on shared SQLite state, and the
        user is expected to know what they're doing.
        """
        discovered = set(self._discover_apps())
        if name not in discovered:
            return {"error": f"Unknown app: {name}"}

        script = HEARTH_ROOT / name / f"{name}.py"
        if not script.exists():
            return {"error": f"Script not found: {script}"}

        # Check Run-using-uv mode. In uv mode, we resolve uv's path and use
        # `uv run <script>` as the spawn command. In no-uv mode, we invoke
        # the script directly via its shebang — the user is responsible for
        # the script being executable and for managing Python and packages.
        use_uv = self._get_use_uv()
        if use_uv:
            uv_path = self._resolve_uv_path()
            if not uv_path:
                return {"error": "uv not found in PATH"}
        else:
            uv_path = None

        # Ask the framework whether a local-mode instance is already
        # running. The framework owns all lockfile logic (where the file
        # lives, how its contents are interpreted, what counts as stale);
        # the monitor just consumes the answer. Returns None if no live
        # instance is found, or a dict with PID/path info if one is.
        import hearth
        existing = hearth.check_existing_lock(name)
        if existing is not None:
            return {"already_open": True, "name": name, "pid": existing["pid"]}
        # No live instance — fall through and spawn. The framework's
        # acquire path will create the lockfile.

        # Read the Terminal preference from hearth.json. Same field that the
        # launcher .desktop file's Terminal= line uses, so the user's choice
        # is consistent across launch paths (start menu icon, desktop icon,
        # OPEN button).
        hearth_config = self._load_hearth_config()
        terminal_enabled = bool((hearth_config.get("apps", {}).get(name, {}) or {}).get("terminal", False))

        # The base command depends on uv mode. In uv mode it's three args
        # (uv, run, script); in no-uv mode it's just the script path.
        base_cmd = [uv_path, "run", str(script)] if use_uv else [str(script)]

        if terminal_enabled:
            # Wrap the base command in a terminal emulator. Different
            # emulators have different invocation syntaxes, so we resolve
            # which one is installed and use the right form.
            term_cmd = self._resolve_terminal_command(base_cmd)
            if term_cmd is None:
                return {"error": "No terminal emulator found (tried "
                                 "x-terminal-emulator, gnome-terminal, xterm)"}
            cmd = term_cmd
        else:
            cmd = base_cmd

        # Spawn detached. start_new_session=True puts the child in its own
        # session group, so it survives independently of the monitor — closing
        # or restarting the monitor won't affect a window the user has open.
        # stdout/stderr to DEVNULL because we're not managing this process.
        # When terminal is enabled, stdio still goes to DEVNULL from the
        # monitor's perspective — the terminal emulator opens its own new
        # window with its own stdio, separate from this Popen pipe.
        #
        # In no-uv mode, env is cleaned to strip uv's contributions (see
        # _clean_env_for_no_uv). Otherwise a uv-managed monitor would leak
        # its venv to the spawned app, masking the user's actual Python
        # config. In uv mode env=None passes through, preserving inheritance.
        spawn_env = None if use_uv else self._clean_env_for_no_uv()
        try:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(HEARTH_ROOT / name),
                env=spawn_env,
            )
        except Exception as e:
            return {"error": str(e)}

        return {"opened": name, "pid": proc.pid, "terminal": terminal_enabled}

    @staticmethod
    def _resolve_terminal_command(base_cmd):
        """Return the argv list to spawn `base_cmd` inside a terminal
        emulator, or None if no suitable emulator is installed.

        `base_cmd` is the argv that would run the app directly. In uv mode
        that's `[uv, "run", script]`; in no-uv mode that's just `[script]`.
        This helper doesn't care which mode — it just wraps whatever it's
        given in the appropriate terminal-emulator invocation.

        Three terminal emulators are tried in order, each with the right
        invocation syntax for that program:

          1. x-terminal-emulator — Debian-standard alternative that resolves
             to whatever the user has set as their default terminal. Uses
             `-e` to specify the command.
          2. gnome-terminal — Cinnamon's typical default. Uses `--` to
             separate the wrapper's own flags from the command to run.
          3. xterm — almost universally available as a last resort. Uses
             `-e` for the command.

        Each emulator opens its own window with its own stdio, where the
        user sees the app's startup output, debug prints, and any tracebacks.
        Useful for debugging an app that's misbehaving on launch."""
        if shutil.which("x-terminal-emulator"):
            return ["x-terminal-emulator", "-e", *base_cmd]
        if shutil.which("gnome-terminal"):
            return ["gnome-terminal", "--", *base_cmd]
        if shutil.which("xterm"):
            return ["xterm", "-e", *base_cmd]
        return None

    def get_logs(self, name, since=0):
        """Get log lines for a managed app since a given ID."""
        since = int(since)
        with self._lock:
            managed = self.managed.get(name)
            if not managed:
                return {"lines": [], "latest_id": 0, "running": False}

            lines = managed.get_logs_since(since)
            latest_id = lines[-1]["id"] if lines else since
            return {
                "lines": lines,
                "latest_id": latest_id,
                "running": managed.is_running(),
                "port": managed.port,
            }

    def _shutdown(self):
        """Terminate all managed child processes on exit."""
        with self._lock:
            for name, managed in list(self.managed.items()):
                try:
                    managed.stop()
                except Exception:
                    pass
            self.managed.clear()


if __name__ == "__main__":
    run(
        HearthMonitor(),
        frontend=str(BASE_DIR / "hearthmonitor.html"),
        title="Hearth Monitor",
        port=8000,
        window={
            "width": 900,
            "height": 650,
            "min_size": (600, 400),
            "background_color": "#0c0c10",
            "text_select": True,
        },
    )
