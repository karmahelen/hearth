"""
Hearth - Run Python app classes as local pywebview windows or web servers.

Usage:
    from hearth import run

    class MyApp:
        def greet(self, name):
            return f"Hello, {name}!"

    if __name__ == "__main__":
        run(MyApp(), frontend="myapp.html", title="My App", port=8080)

    # Local GUI:   [uv run] myapp.py
    # Web server:  [uv run] myapp.py --serve
    # Custom port: [uv run] myapp.py --serve 9090

Authentication (serve mode only):
    Create a hearth.json in the working directory:
    {"password": "your_password_here"}
    When present, serve mode requires login. Local mode is unaffected.

Developer:  KarmaHelen
Contact:    <email>
Support:    https://buymeacoffee.com/karmahelen
"""

import atexit
import hashlib
import inspect
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import traceback
from html import escape as html_escape

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_HANDLER_DIR, "hearth.json")

def _load_config():
    """Load config from hearth.json alongside hearth.py. Returns dict (empty if missing)."""
    if os.path.isfile(_CONFIG_FILE):
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _get_app_alias(app_name, config):
    """Return the user-defined alias for app_name, or None if not set.

    Aliases live under config["apps"][<app_name>]["alias"]. The Hearth Monitor
    edits this; the framework reads it. An empty/whitespace-only alias, or one
    equal to the anchor name, is treated as no alias.
    """
    if not app_name:
        return None
    alias = (config.get("apps", {}).get(app_name, {}) or {}).get("alias")
    if not alias:
        return None
    alias = alias.strip()
    if not alias or alias == app_name:
        return None
    return alias


def _get_app_tray_enabled(app_name, config):
    """Return True if the user has enabled tray support for this app.

    Tray flag lives under config["apps"][<app_name>]["tray"]. Falsy values
    (missing key, False, empty) all mean 'no tray.'"""
    if not app_name:
        return False
    return bool((config.get("apps", {}).get(app_name, {}) or {}).get("tray", False))


def _get_app_terminal_enabled(app_name, config):
    """Return True if the user has enabled terminal-mode launches for this app.

    Terminal flag lives under config["apps"][<app_name>]["terminal"]. Same
    falsy-tolerant shape as tray. The flag is consumed by two paths: launcher
    .desktop file generation (Terminal=true line) and the OPEN button
    (wraps spawn in a terminal emulator). Both read from this same field —
    one preference, two consumers."""
    if not app_name:
        return False
    return bool((config.get("apps", {}).get(app_name, {}) or {}).get("terminal", False))


def _rewrite_html_title(html_content, new_title):
    """Replace the contents of the first <title>...</title> tag in html_content.
    If no <title> tag exists, returns the HTML unchanged. The new_title is
    HTML-escaped before insertion."""
    pattern = re.compile(r'<title>.*?</title>', re.DOTALL | re.IGNORECASE)
    if not pattern.search(html_content):
        return html_content
    replacement = f'<title>{html_escape(new_title)}</title>'
    return pattern.sub(replacement, html_content, count=1)


def _rewrite_hearth_name_elements(html_content, anchor_name, display_name):
    """Replace the anchor name with the display name inside the text of any
    element marked with data-hearth-name.

    The convention (documented in CLAUDE.md): the marked element contains the
    app's anchor name as text — possibly alongside other text or glyphs, but
    with no nested HTML elements. Other content (siblings, attributes,
    surrounding text/glyphs) is preserved untouched.

    The anchor name is matched as a whole word, case-sensitively. The display
    name is HTML-escaped before insertion."""
    if not anchor_name or not display_name:
        return html_content

    # Match an opening tag carrying data-hearth-name, capture the inner text
    # (no nested tags allowed — [^<]* halts at the first '<'), and pair it
    # with the matching closing tag via a backreference to the tag name.
    tag_re = re.compile(
        r'(<(\w+)[^>]*\sdata-hearth-name(?:="[^"]*")?[^>]*>)([^<]*)(</\2\s*>)',
        re.IGNORECASE,
    )
    anchor_re = re.compile(r'\b' + re.escape(anchor_name) + r'\b')
    escaped_display = html_escape(display_name)

    def sub_tag(match):
        opening, _tag_name, inner, closing = match.group(1), match.group(2), match.group(3), match.group(4)
        # Use a lambda for the replacement so any backslashes or digit-prefixed
        # patterns in escaped_display aren't interpreted as backreferences.
        new_inner = anchor_re.sub(lambda _m: escaped_display, inner)
        return opening + new_inner + closing

    return tag_re.sub(sub_tag, html_content)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def _get_api_methods(app):
    """Return a dict of {name: bound_method} for all public methods on app.
    Excludes page_ prefixed methods (those become HTML GET routes)."""
    methods = {}
    for name, func in inspect.getmembers(app, predicate=inspect.ismethod):
        if not name.startswith('_') and not name.startswith('page_'):
            methods[name] = func
    return methods


def _get_page_methods(app):
    """Return a dict of {route_name: bound_method} for page_ prefixed methods.
    page_note becomes GET /page/note, page_dashboard becomes GET /page/dashboard, etc."""
    methods = {}
    for name, func in inspect.getmembers(app, predicate=inspect.ismethod):
        if name.startswith('page_'):
            route_name = name[5:]  # strip 'page_' prefix
            methods[route_name] = func
    return methods


# ---------------------------------------------------------------------------
# JS shim — always uses fetch since both modes run a server
# ---------------------------------------------------------------------------

_JS_SHIM = """
<script>
const app = {
    call: async (method, params = {}) => {
        const res = await fetch('/api/' + method, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(params)
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return;
        }
        const response = await res.json();
        if (!response.ok) throw new Error(response.error);
        return response.result;
    },
    upload: async (file) => {
        const formData = new FormData();
        formData.append('file', file);
        const res = await fetch('/upload', {
            method: 'POST',
            body: formData
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return;
        }
        const response = await res.json();
        if (!response.ok) throw new Error(response.error);
        return response.result;
    },
    pickAndUpload: (accept) => {
        return new Promise((resolve, reject) => {
            const input = document.createElement('input');
            input.type = 'file';
            if (accept) input.accept = accept;
            input.onchange = async () => {
                if (!input.files.length) { resolve(null); return; }
                try { resolve(await app.upload(input.files[0])); }
                catch (e) { reject(e); }
            };
            input.click();
        });
    }
};
</script>
"""


def _inject_shim(html):
    """Inject the JS shim into the HTML string."""
    if '</head>' in html:
        return html.replace('</head>', _JS_SHIM + '\n</head>')
    elif '<body' in html:
        return _JS_SHIM + '\n' + html
    else:
        return _JS_SHIM + '\n' + html


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

_LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #1a1a2e; color: #e0e0e0;
        display: flex; align-items: center; justify-content: center;
        min-height: 100vh;
        padding: 16px;
    }
    .login-box {
        background: #16213e; border: 1px solid #0f3460;
        border-radius: 8px; padding: 32px;
        width: 100%; max-width: 360px;
    }
    .login-box h2 {
        font-size: 18px; color: #a0c4ff; margin-bottom: 20px;
        text-align: center;
    }
    /* 16px on inputs prevents iOS Safari auto-zoom on focus, which would
       otherwise jump the layout when the keyboard appears. */
    .login-box input[type="password"] {
        width: 100%; padding: 10px 12px; border-radius: 4px;
        border: 1px solid #0f3460; background: #1a1a2e; color: #e0e0e0;
        font-size: 16px; margin-bottom: 16px; outline: none;
    }
    .login-box input[type="password"]:focus {
        border-color: #a0c4ff;
    }
    .login-box button {
        width: 100%; padding: 10px; border-radius: 4px; border: none;
        background: #0f3460; color: #a0c4ff; font-size: 14px;
        cursor: pointer;
    }
    .login-box button:hover { background: #1a4f8a; }
    .error {
        color: #ff8080; font-size: 13px; text-align: center;
        margin-bottom: 12px;
    }
    /* Narrow screens: scale up the title for stronger visual anchoring,
       give the input/button more vertical room for comfortable touch
       targets, and let the box span the available width with breathing
       room from the page padding. */
    @media (max-width: 600px) {
        .login-box {
            padding: 24px 20px;
        }
        .login-box h2 {
            font-size: 20px;
            margin-bottom: 24px;
        }
        .login-box input[type="password"] {
            padding: 14px 14px;
            margin-bottom: 18px;
        }
        .login-box button {
            padding: 14px;
            font-size: 15px;
            min-height: 48px;
        }
    }
</style>
</head>
<body>
<div class="login-box">
    <h2>%%TITLE%%</h2>
    %%ERROR%%
    <input type="password" id="pw" placeholder="Password" autofocus
           onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Log In</button>
</div>
<script>
async function doLogin() {
    const pw = document.getElementById('pw').value;
    const res = await fetch('/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({password: pw})
    });
    if (res.ok) {
        window.location.href = '/';
    } else {
        const data = await res.json();
        document.getElementById('pw').value = '';
        document.getElementById('pw').placeholder = data.error || 'Try again';
        document.getElementById('pw').focus();
    }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Response wrapping
# ---------------------------------------------------------------------------

def _wrap_call(func, params):
    """Call func with params dict unpacked as kwargs, return standard envelope."""
    try:
        if params:
            result = func(**params)
        else:
            result = func()
        return {"ok": True, "result": result}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Flask server builder
# ---------------------------------------------------------------------------

def _create_server(app, methods, page_methods, html, static_dir, config, enable_auth, display_name):
    """Build and return a Flask app with API routes and static file serving."""
    from flask import Flask, request, jsonify, Response, session, redirect, send_from_directory

    server = Flask(__name__)

    # Authentication (serve mode only)
    password = config.get("password") if enable_auth else None

    if password:
        server.secret_key = hashlib.sha256(
            f"Hearth:{password}".encode()
        ).hexdigest()

        @server.before_request
        def check_auth():
            if request.path == '/login':
                return None
            if not session.get('authenticated'):
                if request.path.startswith('/api/') or request.path == '/upload' or request.path.startswith('/page/'):
                    return jsonify({"ok": False, "error": "Not authenticated"}), 401
                return redirect('/login')
            return None

        @server.route('/login', methods=['GET'])
        def login_page():
            page = _LOGIN_PAGE.replace('%%TITLE%%', html_escape(display_name))
            page = page.replace('%%ERROR%%', '')
            return Response(page, mimetype='text/html')

        @server.route('/login', methods=['POST'])
        def login_submit():
            data = request.get_json(silent=True) or {}
            if data.get('password') == password:
                session['authenticated'] = True
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": "Wrong password"}), 401

        print("[Hearth] Authentication enabled")

    # Main page
    @server.route('/')
    def index():
        return Response(html, mimetype='text/html')

    # Favicon — auto-serve if a .png or .ico icon file exists in the app directory
    if static_dir:
        _favicon = None
        _favicon_mime = None
        for _ext, _mime in [('.png', 'image/png'), ('.ico', 'image/x-icon')]:
            _candidates = [f for f in os.listdir(static_dir) if f.lower().endswith(_ext)]
            if _candidates:
                _favicon = _candidates[0]
                _favicon_mime = _mime
                break
        if _favicon:
            _fav_file = _favicon
            _fav_mime = _favicon_mime
            @server.route('/favicon.ico')
            def favicon():
                return send_from_directory(static_dir, _fav_file, mimetype=_fav_mime)

    # Static files (CSS, JS, images, fonts, etc.) — whitelisted extensions only
    # Note: .json is intentionally excluded — config files and API keys live in
    # .json files and must never be served. Use API routes to serve JSON data.
    _SAFE_EXTENSIONS = {
        '.css', '.js', '.mjs',
        '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico',
        '.woff', '.woff2', '.ttf', '.otf', '.eot',
        '.html', '.htm',
        '.map',
        '.mp3', '.wav', '.ogg', '.mp4', '.webm',
        '.pdf',
    }

    if static_dir:
        @server.route('/<path:filename>')
        def static_files(filename):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _SAFE_EXTENSIONS:
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            return send_from_directory(static_dir, filename)

    # File upload route (only if app has handle_upload method)
    handle_upload = getattr(app, 'handle_upload', None)
    if handle_upload and callable(handle_upload):
        @server.route('/upload', methods=['POST'])
        def upload():
            if 'file' not in request.files:
                return jsonify({"ok": False, "error": "No file provided"}), 400
            uploaded = request.files['file']
            if not uploaded.filename:
                return jsonify({"ok": False, "error": "Empty filename"}), 400

            # Save to a temp file preserving the original extension
            _, ext = os.path.splitext(uploaded.filename)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            try:
                uploaded.save(tmp)
                tmp.close()
                result = _wrap_call(handle_upload, {
                    'filename': uploaded.filename,
                    'filepath': tmp.name
                })
                return jsonify(result)
            finally:
                # Clean up temp file if handle_upload didn't move it
                if os.path.exists(tmp.name):
                    os.unlink(tmp.name)

    # API routes
    for method_name, method_func in methods.items():
        def make_handler(func):
            def handler():
                params = request.get_json(silent=True) or {}
                result = _wrap_call(func, params)
                return jsonify(result)
            handler.__name__ = f"api_{func.__name__}"
            return handler
        server.add_url_rule(
            f'/api/{method_name}',
            endpoint=f'api_{method_name}',
            view_func=make_handler(method_func),
            methods=['POST']
        )

    # Page routes (page_ methods → GET /page/<name> returning HTML)
    for route_name, page_func in page_methods.items():
        def make_page_handler(func):
            def handler(**kwargs):
                params = dict(request.args)
                params.update(kwargs)
                try:
                    if params:
                        result = func(**params)
                    else:
                        result = func()
                    if result is None:
                        return Response("Not found", status=404)
                    return Response(result, mimetype='text/html')
                except Exception as e:
                    traceback.print_exc()
                    return Response(f"Error: {e}", status=500)
            handler.__name__ = f"page_{func.__name__}"
            return handler
        server.add_url_rule(
            f'/page/{route_name}',
            endpoint=f'page_{route_name}',
            view_func=make_page_handler(page_func),
            methods=['GET']
        )

    return server


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _find_free_port():
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Single-instance lockfile (local mode only)
# ---------------------------------------------------------------------------
#
# Hearth apps shouldn't run as multiple simultaneous instances on one
# machine — they share state (SQLite databases, config, log files), and
# concurrent writes can corrupt that state. Pre-tray-support, double-launches
# were silently self-correcting because the second instance would crash on
# port-binding for fixed-port apps. But local-mode apps use ephemeral ports
# (each instance gets a different one), so two instances can fully start
# alongside each other without ever colliding. The tray icon made this
# pre-existing problem visible (two icons = two running monitors); without
# tray, the second instance was just an invisible process leaking resources.
#
# The fix is an explicit lockfile check in local mode. Serve mode is left
# alone — that's the "you know what you're doing" path, often deployed as
# a systemd service or in scripts where automation handles state better.
#
# Lockfile path: <tempdir>/hearth-<appname>.lock
# Lockfile contents: <pid>\n<script_path>\n
#
# We use tempfile.gettempdir() rather than hardcoding /tmp so the lockfile
# location is consistent with the rest of the framework (file uploads also
# use tempfile.gettempdir() via NamedTemporaryFile) and honors $TMPDIR if
# the user has set it. Under normal Linux configs, this resolves to /tmp,
# which the OS clears on reboot — a useful bonus that takes care of stale
# lockfiles from any process that died across a reboot boundary. But we
# don't *depend* on that clearing for correctness: even if the lockfile
# survives somehow, the in-process detection below handles it.
#
# On launch, if the lockfile exists, we verify the recorded PID is still
# alive AND its /proc/<pid>/cmdline contains our script's basename. Both
# true → refuse to start. Either false → lock is stale, overwrite and
# continue. The cmdline check protects against PID reuse: the kernel may
# have given the dead instance's PID to an unrelated process, which we
# don't want to treat as "our app is already running."

def _lockfile_path(app_name):
    """Return the path to the lockfile for app_name."""
    return os.path.join(tempfile.gettempdir(), f"hearth-{app_name}.lock")


def _is_pid_running_our_app(pid, script_path):
    """Return True if the given PID is alive AND its cmdline references
    the same script. Either condition false → lock can be reclaimed.

    On Linux we read /proc/<pid>/cmdline. The cmdline file is null-separated;
    we check whether the script's *basename* appears in it. We deliberately
    don't compare full paths, because cmdline reflects however the script
    was invoked — `uv run /full/path/to/script.py` vs `python script.py`
    vs `./script.py` produce different cmdline contents even though they're
    all the same app. Basename matching is unique enough in practice (every
    Hearth app's script is named after its directory, and directory names
    must be unique) and resilient to invocation-style differences.
    """
    try:
        # Sending signal 0 is the standard "is this PID alive?" check —
        # raises OSError if the process doesn't exist.
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False  # not alive → stale lock

    cmdline_path = f"/proc/{pid}/cmdline"
    try:
        with open(cmdline_path, "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError):
        # /proc entry vanished between our kill check and read, OR we lack
        # permission to read it (different user). Either way, we can't
        # confirm it's our app — treat as stale to be safe.
        return False

    script_basename = os.path.basename(script_path) if script_path else ""
    if not script_basename:
        return False
    return script_basename in cmdline


def check_existing_lock(app_name):
    """Inspect the lockfile for app_name without trying to acquire it.

    This is the read-only counterpart to _acquire_single_instance_lock,
    used by callers (like Hearth Monitor's OPEN button) that need to know
    whether an instance is already running but don't intend to start one
    themselves. The framework's acquire path uses this same function to
    determine whether a lockfile is stale, so there's exactly one place
    that knows how to interpret the file format.

    Returns:
      None — no lockfile, or the lockfile is stale (PID dead, or alive
             but running a different program). A caller wanting to start
             a new instance can proceed.
      dict — {"pid": int, "script_path": str, "lock_path": str} when a
             live local-mode instance is running. The caller should not
             start a new instance.
    """
    lock_path = _lockfile_path(app_name)
    if not os.path.exists(lock_path):
        return None

    try:
        with open(lock_path) as f:
            lines = f.read().strip().splitlines()
        existing_pid = int(lines[0]) if lines else 0
        existing_path = lines[1] if len(lines) > 1 else ""
    except (ValueError, OSError):
        # Malformed lockfile — treat as stale; the file will be overwritten
        # on next acquire.
        return None

    if existing_pid and _is_pid_running_our_app(existing_pid, existing_path):
        return {
            "pid": existing_pid,
            "script_path": existing_path,
            "lock_path": lock_path,
        }
    return None


def _acquire_single_instance_lock(app_name, script_path):
    """Try to acquire the single-instance lock for app_name. Returns the
    lockfile path on success, or None if another instance is already running.

    Stale lockfiles (PID dead, or PID alive but running a different program)
    are reclaimed silently — no point pestering the user about previous
    crashes that left a file behind."""
    existing = check_existing_lock(app_name)
    if existing is not None:
        print(f"[Hearth] {app_name} is already running (PID {existing['pid']}).",
              file=sys.stderr)
        print(f"[Hearth] Look for its icon in the system tray, or check "
              f"{existing['lock_path']} for details.", file=sys.stderr)
        return None

    lock_path = _lockfile_path(app_name)
    try:
        with open(lock_path, "w") as f:
            f.write(f"{os.getpid()}\n{script_path}\n")
    except OSError as e:
        # Couldn't write the lockfile — disk full, permission issue, etc.
        # We don't want to refuse to start the app over this; warn and
        # continue without single-instance protection.
        print(f"[Hearth] Warning: could not write lockfile {lock_path}: {e}",
              file=sys.stderr)
        return None

    return lock_path


def _release_single_instance_lock(lock_path):
    """Remove the lockfile on clean exit. Tolerant of the file already being
    gone (some other cleanup path may have removed it, or the process may
    be exiting twice somehow)."""
    if not lock_path:
        return
    try:
        os.unlink(lock_path)
    except (OSError, FileNotFoundError):
        pass


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _shutdown_app(app):
    """Call the app's _shutdown method if it exists. Called once on exit."""
    shutdown = getattr(app, '_shutdown', None)
    if shutdown and callable(shutdown):
        try:
            shutdown()
            print("[Hearth] App shutdown complete")
        except Exception:
            traceback.print_exc()


def _make_letter_icon(letter, size=64):
    """Render a simple letter-on-colored-square as a fallback tray icon for
    apps without their own .ico file. Uses a hash of the letter to pick a
    stable color per app, so different apps with no .ico still get visually
    distinguishable tray icons rather than a single generic icon."""
    from PyQt6.QtGui import QPixmap, QIcon, QPainter, QColor, QFont
    from PyQt6.QtCore import Qt

    # Stable color from the letter — same letter always produces the same
    # color, different letters produce different colors. Limited to a darker
    # palette so white text reads cleanly against it.
    h = hash(letter.upper()) & 0xFFFFFF
    r = 60 + (h & 0x7F)
    g = 60 + ((h >> 8) & 0x7F)
    b = 60 + ((h >> 16) & 0x7F)
    bg = QColor(r, g, b)

    pixmap = QPixmap(size, size)
    pixmap.fill(bg)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(255, 255, 255))
    font = QFont()
    font.setPointSize(int(size * 0.55))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter[:1].upper())
    painter.end()
    return QIcon(pixmap)


def _setup_tray(qapp, get_window, app_name, display_name, icon_path):
    """Set up the system tray icon and menu for the running app.

    qapp        — the QApplication instance (must exist before this is called)
    get_window  — callable returning the pywebview window (deferred because the
                  window doesn't exist yet when tray setup runs)
    app_name    — anchor name (used for the fallback letter icon)
    display_name — alias-or-anchor (used for the tooltip)
    icon_path   — path to the app's .ico/.png, or None for letter fallback

    Returns a (tray, mark_hidden, mark_shown) tuple. The two callables let the
    caller report when the window has been hidden or shown, so the tray's
    visibility state stays accurate. We track visibility ourselves rather than
    polling pywebview's `window.hidden` attribute, which has been observed to
    not update reliably after `window.hide()` calls.

    The QSystemTrayIcon must be kept alive by the caller (Qt requires it — if
    the icon goes out of scope, it disappears from the tray)."""
    from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
    from PyQt6.QtGui import QIcon, QAction

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("[Hearth] System tray not available; tray icon disabled.")
        return None, None, None

    # When tray is enabled, the QApplication must NOT quit when the window is
    # hidden — otherwise the tray icon dies the moment the user closes the
    # window. Quit happens only via the tray menu's Quit entry, which calls
    # qapp.quit() explicitly.
    qapp.setQuitOnLastWindowClosed(False)

    if icon_path:
        icon = QIcon(icon_path)
    else:
        icon = _make_letter_icon(app_name)

    tray = QSystemTrayIcon(icon)
    tray.setToolTip(display_name)

    # Lifetime management for the menu and actions. QMenu requires a QWidget
    # parent, but QSystemTrayIcon is a QObject (not a QWidget) — so we can't
    # use it as the menu's Qt parent. Instead:
    #   - QActions are parented to the QApplication, which is a QObject and
    #     outlives everything else in the process.
    #   - The QMenu has no Qt parent and is kept alive purely via the Python
    #     attribute attachment below, alongside the closures.
    # Without these protections, the menu and actions go out of scope when
    # this function returns; Qt's C++ side then holds dangling pointers, and
    # depending on the desktop environment, items silently fail to render or
    # signals may not fire.
    menu = QMenu()
    # Single toggling Show/Hide entry — text updates based on current window
    # state at menu-open time, so the user always sees the action they want.
    toggle_action = QAction("Show", parent=qapp)
    quit_action = QAction("Quit", parent=qapp)
    menu.addAction(toggle_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    # Track window visibility ourselves rather than polling pywebview's
    # `window.hidden` attribute. The attribute doesn't reliably reflect
    # state changes from .hide() calls in the version of pywebview we're
    # using — the window genuinely hides but the flag stays at its initial
    # value, which made the toggle entry stuck on "Hide" and made
    # left-click a no-op. Tracking visibility from our own callbacks is
    # more reliable: we update the flag exactly when we change visibility,
    # so it can never drift from reality.
    state = {"visible": True}  # window starts shown when pywebview launches it

    def is_window_visible():
        return state["visible"]

    def show_window():
        win = get_window()
        if win is not None:
            try:
                win.show()
                state["visible"] = True
            except Exception:
                pass

    def hide_window():
        win = get_window()
        if win is not None:
            try:
                win.hide()
                state["visible"] = False
            except Exception:
                pass

    def toggle_window():
        if is_window_visible():
            hide_window()
        else:
            show_window()

    def on_menu_about_to_show():
        # Update the toggle entry's label every time the menu is opened —
        # gives the user the action they'd expect rather than guessing.
        toggle_action.setText("Hide" if is_window_visible() else "Show")

    def on_tray_activated(reason):
        # Left-click (Trigger reason) toggles window visibility — the
        # one-click affordance we want for the icon. Right-click (Context
        # reason) is handled separately by Qt to show the menu, no
        # additional code needed.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            toggle_window()

    toggle_action.triggered.connect(toggle_window)
    quit_action.triggered.connect(qapp.quit)
    menu.aboutToShow.connect(on_menu_about_to_show)
    tray.activated.connect(on_tray_activated)

    # Belt-and-suspenders Python attribute attachment to keep both the menu
    # and the closures alive. The QMenu has no Qt parent (QSystemTrayIcon
    # isn't a QWidget, so we can't use it), so this is the *only* thing
    # keeping the menu alive — without this line, the menu goes out of
    # scope when this function returns. Closures aren't Qt objects so
    # parent-child ownership wouldn't apply to them anyway; they need a
    # plain Python reference somewhere to survive.
    tray._hearth_menu = menu
    tray._hearth_closures = (
        is_window_visible, show_window, hide_window, toggle_window,
        on_menu_about_to_show, on_tray_activated,
    )

    tray.show()

    # Expose the state-mutation hooks so the caller (run()) can mark the
    # window hidden when the user clicks the X. Only the hidden hook is
    # currently needed — show_window already updates state when it runs.
    def mark_hidden():
        state["visible"] = False

    def mark_shown():
        state["visible"] = True

    return tray, mark_hidden, mark_shown


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(app, frontend, title="App", port=8080, host="0.0.0.0", window=None):
    """
    Run the app in either serve mode (--serve) or local pywebview mode.

    Both modes run a Flask server. In local mode, the server binds to
    127.0.0.1 on an ephemeral port and pywebview opens a native window
    pointed at it. In serve mode, the server binds to 0.0.0.0 (or the
    specified host) for network access, with optional authentication.

    Args:
        app:      Instance of your app class. Public methods become API endpoints.
        frontend: Path to an HTML file, or a string of HTML.
        title:    Window title (local mode only).
        port:     Port to listen on (serve mode only).
        host:     Host to bind to (serve mode only). Default 0.0.0.0 for LAN access.
        window:   Dict of pywebview window options (local mode only). Supported keys:
                  width, height, min_size, background_color, text_select,
                  resizable, fullscreen, frameless, on_top, icon.
                  If icon is not set, auto-detects from .ico or .png in app directory.
    """
    serve_mode = '--serve' in sys.argv
    if window is None:
        window = {}

    # Check for --serve <port>
    if serve_mode:
        idx = sys.argv.index('--serve')
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1].isdigit():
            port = int(sys.argv[idx + 1])

    # Load config
    config = _load_config()

    # Load frontend HTML and determine static file directory
    static_dir = None
    app_name = None
    if os.path.isfile(frontend):
        static_dir = os.path.dirname(os.path.abspath(frontend))
        # Derive the app's anchor name from its directory (convention:
        # appname/appname.html), used to look up the user-defined alias.
        app_name = os.path.basename(static_dir)
        with open(frontend, 'r', encoding='utf-8') as f:
            html = f.read()
    else:
        html = frontend

    # Resolve the user-facing display name. The alias (if set in hearth.json)
    # overrides the developer-declared title= argument across every surface
    # the user sees: pywebview window, browser tab, login page.
    alias = _get_app_alias(app_name, config)
    display_name = alias if alias else title

    # Read the user's tray preference from hearth.json. Only meaningful in
    # local mode — serve mode has no desktop and ignores the flag.
    tray_enabled = _get_app_tray_enabled(app_name, config)

    # Inject the JS shim
    html = _inject_shim(html)

    # Rewrite the HTML's <title> tag and any [data-hearth-name] elements only
    # when an alias is set — without an alias, the developer's authored content
    # wins untouched at every level.
    if alias:
        html = _rewrite_html_title(html, alias)
        html = _rewrite_hearth_name_elements(html, app_name, alias)

    # Discover API methods and page methods
    methods = _get_api_methods(app)
    page_methods = _get_page_methods(app)

    if not methods and not page_methods:
        print("[Hearth] Warning: no public methods found on app class.")

    # Build the Flask server (auth only in serve mode)
    server = _create_server(app, methods, page_methods, html, static_dir, config,
                            enable_auth=serve_mode, display_name=display_name)

    # Register cleanup — atexit is a safety net, try/finally is the primary path.
    # Use a flag to ensure _shutdown only runs once.
    _shutdown_done = []

    def do_shutdown():
        if not _shutdown_done:
            _shutdown_done.append(True)
            _shutdown_app(app)

    atexit.register(do_shutdown)

    # Handle SIGTERM (e.g., systemd stop, kill) the same as Ctrl+C
    def sigterm_handler(signum, frame):
        print("\n[Hearth] Received SIGTERM, shutting down...")
        do_shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, sigterm_handler)

    if serve_mode:
        print(f"[Hearth] Serving on http://{host}:{port}")
        print(f"[Hearth] API endpoints:")
        for name in sorted(methods.keys()):
            print(f"  POST /api/{name}")
        if hasattr(app, 'handle_upload') and callable(app.handle_upload):
            print(f"  POST /upload")
        for name in sorted(page_methods.keys()):
            print(f"  GET  /page/{name}")
        print()
        try:
            server.run(host=host, port=port, debug=False)
        except KeyboardInterrupt:
            print("\n[Hearth] Interrupted")
        finally:
            do_shutdown()
    else:
        import webview

        # Single-instance guard for local mode. If another instance is
        # already running (verified via PID + cmdline check, so PID reuse
        # doesn't fool us), refuse to start. Stale lockfiles from earlier
        # crashes are reclaimed silently. Serve mode skips this — that path
        # is for headless/scripted use where the caller manages state.
        script_path = os.path.abspath(sys.argv[0]) if sys.argv else ""
        lock_path = _acquire_single_instance_lock(app_name or "app", script_path)
        if lock_path is None and app_name:
            # Couldn't acquire (another instance is running). Exit cleanly
            # without running do_shutdown() — there's nothing of ours that
            # needs cleaning up since we never started.
            sys.exit(1)

        # Register the lock release as an atexit handler too. This catches
        # exit paths the explicit `finally` below might miss — uncaught
        # exceptions that propagate past run(), os._exit() from deep in a
        # library, or signal handlers that exit the process. atexit isn't
        # foolproof (it doesn't run on SIGKILL or hard crashes), but it
        # covers everything short of those.
        atexit.register(_release_single_instance_lock, lock_path)

        # Start Flask in a background thread on localhost
        local_port = _find_free_port()
        thread = threading.Thread(
            target=lambda: server.run(
                host='127.0.0.1', port=local_port, debug=False, use_reloader=False
            ),
            daemon=True
        )
        thread.start()

        print(f"[Hearth] Local server on http://127.0.0.1:{local_port}")

        # Auto-detect icon from app directory (prefer .png over .ico)
        icon_path = window.get('icon')
        if not icon_path and static_dir:
            for ext in ('.png', '.ico'):
                candidates = [f for f in os.listdir(static_dir) if f.lower().endswith(ext)]
                if candidates:
                    icon_path = os.path.join(static_dir, candidates[0])
                    break

        try:
            # When tray support is enabled, build the QApplication ourselves
            # before pywebview starts. pywebview's Qt backend will reuse the
            # existing QApplication instance, which means the tray icon and
            # the pywebview window share the same event loop with no thread
            # coordination needed.
            #
            # Important Qt constraint: QtWebEngineWidgets requires either
            # to be imported BEFORE the QApplication is created, OR the
            # AA_ShareOpenGLContexts attribute to be set on QApplication
            # before instantiation. We use the second option — it's the
            # Qt-documented approach and avoids importing a heavy module
            # (WebEngine) here just to satisfy initialization order. Without
            # this, pywebview's Qt backend fails on import with:
            #   "QtWebEngineWidgets must be imported or
            #    Qt.AA_ShareOpenGLContexts must be set before a
            #    QCoreApplication instance is created"
            tray_icon_ref = None
            qapp = None
            if tray_enabled:
                from PyQt6.QtCore import Qt as _Qt
                from PyQt6.QtWidgets import QApplication
                QApplication.setAttribute(_Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
                qapp = QApplication.instance() or QApplication(sys.argv)
                # Suppress the noisy AT-SPI accessibility warning that Qt6
                # emits on Cinnamon. Cosmetic — has no effect on tray or
                # window functionality.
                os.environ.setdefault("QT_LOGGING_RULES", "qt.accessibility.atspi.warning=false")

            # Open pywebview pointed at the local server
            win = webview.create_window(
                display_name,
                url=f'http://127.0.0.1:{local_port}',
                width=window.get('width', 1024),
                height=window.get('height', 768),
                min_size=window.get('min_size', (200, 100)),
                background_color=window.get('background_color', '#FFFFFF'),
                text_select=window.get('text_select', False),
                resizable=window.get('resizable', True),
                fullscreen=window.get('fullscreen', False),
                frameless=window.get('frameless', False),
                on_top=window.get('on_top', False),
            )

            if tray_enabled:
                # Build the tray icon first so we have the mark_hidden hook
                # available to the close handler. Keep a Python reference
                # (tray_icon_ref) so the QSystemTrayIcon isn't garbage-
                # collected — Qt requires this.
                tray_icon_ref, mark_hidden, mark_shown = _setup_tray(
                    qapp,
                    get_window=lambda: win,
                    app_name=app_name or "App",
                    display_name=display_name,
                    icon_path=icon_path,
                )

                # Intercept the window's close event: hide instead of close
                # so the process stays alive with the tray icon visible.
                # Returning False from this handler cancels the close. We
                # also notify the tray's visibility tracker so the toggle
                # menu entry's label and left-click behavior reflect the
                # actual state.
                def _on_closing():
                    win.hide()
                    if mark_hidden:
                        mark_hidden()
                    return False
                # The pywebview events API has shifted across versions. The
                # `+=` subscription is the documented pattern for 4.x; if the
                # installed version expects a different syntax, the fallback
                # below tries the direct-call form.
                try:
                    win.events.closing += _on_closing
                except Exception:
                    try:
                        win.events.closing(_on_closing)
                    except Exception as e:
                        print(f"[Hearth] could not subscribe to close event: {e}", flush=True)

            webview.start(icon=icon_path)
        finally:
            do_shutdown()
            # Release the single-instance lockfile on the way out. Even
            # if do_shutdown raised, we still want to release — leaving
            # stale lockfiles around makes future launches awkward (the
            # next launch will reclaim it on the stale-PID check, but
            # the user might briefly worry about the warning if we ever
            # add one).
            _release_single_instance_lock(lock_path)
            # Daemon thread dies when main thread exits
