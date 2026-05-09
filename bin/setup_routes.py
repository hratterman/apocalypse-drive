"""
Setup wizard + admin endpoints for the Apocalypse drive.

Plugs into kiwix_shim.py via dispatch_setup() / dispatch_setup_post().

Routes:
  GET  /setup                  -> setup wizard HTML (first-run)
  GET  /admin                  -> admin/library management HTML
  GET  /api/catalog            -> JSON catalog (curated ZIMs + bundles), live-hydrated
  GET  /api/catalog?fresh=1    -> force re-fetch from Kiwix (skip cache)
  GET  /api/status             -> JSON system status (installed ZIMs, downloads, services)
  GET  /api/disk?path=<p>      -> JSON {free, total} for given path
  GET  /api/browse?q=&page=    -> live Kiwix library search/browse (paginated)
  POST /api/download/start     -> start downloads {ids: [...], dest_dir: "...", model: "3b"|"8b"|"none"}
  POST /api/download/start_url -> start a download by direct URL {url, name, size}
  GET  /api/download/progress  -> JSON list of {id, name, total, downloaded, status}
  POST /api/download/cancel    -> cancel a running download {id: "..."}
  POST /api/download/retry     -> restart an errored or cancelled download {id} (resumes via Range header)
  POST /api/download/clear     -> drop an errored/cancelled entry + delete .part {id}
  POST /api/download/remove    -> delete an installed ZIM {id: "..."} (frees disk)
  POST /api/services/restart   -> restart kiwix-serve / shim to pick up new ZIMs
  GET  /api/config             -> JSON current config (theme, model, install_dir, etc.)
  POST /api/config             -> update config

State lives in <install_dir>/state.json. Downloads run in background threads
using urllib (no external deps). Resume via Range header.
"""
import json
import sys
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Sibling module for OPDS access. Optional: if it fails to import or its
# network calls fail, the whole app degrades gracefully to baked-in catalog.
try:
    from . import kiwix_opds  # type: ignore
except ImportError:
    try:
        import kiwix_opds  # type: ignore
    except ImportError:
        kiwix_opds = None  # type: ignore

# --- Module state -----------------------------------------------------------

_INSTALL_DIR = None        # Path: where the apocalypse drive lives
_CATALOG = None            # dict: parsed catalog.json (baked-in)
_HYDRATED_CATALOG = None   # dict: catalog + live OPDS field overrides
_HYDRATED_AT = 0.0         # epoch seconds of last successful hydration
_HYDRATION_LOCK = threading.Lock()
_DOWNLOADS = {}            # id -> Download object
_DOWNLOADS_LOCK = threading.Lock()
_SHIM_RESTART_HOOK = None  # callable: triggers shim to reload ZIMs
_ZIM_DIR_RELOCATE_HOOK = None  # callable(new_zim_dir): updates kiwix_shim.ZIM_DIR + ARCHIVES
_RELOAD_TIMER = None       # debounce timer for shim reloads
_RELOAD_LOCK = threading.Lock()


def _schedule_shim_reload(delay=2.0):
    """Schedule a shim ZIM reload, debounced.

    Multiple ZIMs finishing in quick succession collapse into a single
    reload after the last one. Without debouncing, a 4-ZIM batch would
    trigger 4 sequential ARCHIVES rebuilds, each scanning every .zim file.
    """
    global _RELOAD_TIMER
    if _SHIM_RESTART_HOOK is None:
        return
    with _RELOAD_LOCK:
        if _RELOAD_TIMER is not None:
            try:
                _RELOAD_TIMER.cancel()
            except Exception:
                pass
        _RELOAD_TIMER = threading.Timer(delay, _SHIM_RESTART_HOOK)
        _RELOAD_TIMER.daemon = True
        _RELOAD_TIMER.start()


def init(install_dir, catalog_path, restart_hook=None, relocate_hook=None):
    """Call once at shim startup."""
    global _INSTALL_DIR, _CATALOG, _SHIM_RESTART_HOOK, _ZIM_DIR_RELOCATE_HOOK
    _INSTALL_DIR = Path(install_dir).resolve()
    _SHIM_RESTART_HOOK = restart_hook
    _ZIM_DIR_RELOCATE_HOOK = relocate_hook
    with open(catalog_path) as f:
        _CATALOG = json.load(f)
    # Ensure dirs exist
    (_INSTALL_DIR / 'kiwix' / 'zim').mkdir(parents=True, exist_ok=True)
    (_INSTALL_DIR / 'llm').mkdir(parents=True, exist_ok=True)
    (_INSTALL_DIR / 'logs').mkdir(parents=True, exist_ok=True)

    # Kick off background hydration so first wizard request is fast
    if kiwix_opds is not None:
        threading.Thread(target=_hydrate_in_background, daemon=True).start()


def relocate_install_dir(new_path):
    """Move the install_dir target to a new path without restarting the process.

    Safe only when no downloads are in flight and no ZIMs have been installed
    yet. The caller (HTTP handler) is responsible for that gate. Updates
    _INSTALL_DIR, recreates the standard subdirs at the new location, and
    notifies the shim's ZIM_DIR via the relocate hook so subsequent search /
    download writes land at the new place.
    """
    global _INSTALL_DIR
    new_path = Path(new_path).expanduser().resolve()
    new_path.mkdir(parents=True, exist_ok=True)
    new_zim = new_path / 'kiwix' / 'zim'
    new_zim.mkdir(parents=True, exist_ok=True)
    (new_path / 'llm').mkdir(parents=True, exist_ok=True)
    (new_path / 'logs').mkdir(parents=True, exist_ok=True)
    _INSTALL_DIR = new_path
    if _ZIM_DIR_RELOCATE_HOOK:
        try:
            _ZIM_DIR_RELOCATE_HOOK(str(new_zim))
        except Exception:
            pass


def _hydrate_in_background():
    """Fetch live OPDS data and update _HYDRATED_CATALOG. Safe to fail."""
    global _HYDRATED_CATALOG, _HYDRATED_AT
    if kiwix_opds is None or _CATALOG is None:
        return
    try:
        result = kiwix_opds.hydrate_catalog(_CATALOG)
        with _HYDRATION_LOCK:
            _HYDRATED_CATALOG = result
            _HYDRATED_AT = time.time()
    except Exception:
        # Network down, DNS, etc. Silent fail; serve baked-in catalog.
        pass


def _serve_catalog(force_fresh=False):
    """
    Return the hydrated catalog if we have one, else the baked-in catalog.
    If force_fresh=True, blocks for one synchronous re-hydration attempt.
    """
    if force_fresh and kiwix_opds is not None:
        _hydrate_in_background()  # synchronous when called inline (not in thread)
        # Actually do it synchronously:
        try:
            result = kiwix_opds.hydrate_catalog(_CATALOG)
            with _HYDRATION_LOCK:
                global _HYDRATED_CATALOG, _HYDRATED_AT
                _HYDRATED_CATALOG = result
                _HYDRATED_AT = time.time()
        except Exception:
            pass

    with _HYDRATION_LOCK:
        if _HYDRATED_CATALOG is not None:
            return _HYDRATED_CATALOG
    return _CATALOG


def is_initialized():
    return _INSTALL_DIR is not None and _CATALOG is not None


# --- State file -------------------------------------------------------------

def _state_path():
    return _INSTALL_DIR / 'state.json'


def load_state():
    p = _state_path()
    if not p.exists():
        return {
            'setup_complete': False,
            'theme': 'terminal',
            'model': '3b',
            'installed_ids': [],
        }
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {'setup_complete': False, 'theme': 'terminal', 'model': '3b', 'installed_ids': []}


def save_state(state):
    p = _state_path()
    tmp = p.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.replace(p)


def _persist_state_after_download():
    """Mirror disk-derived installed_ids into state.json so theme/model
    preferences from the wizard survive across restarts even if the user
    never explicitly clicked 'Setup Complete'. This is a soft-persistence
    layer: installed_ids() always returns the truth from disk, but state.json
    tracks user preferences (theme, chosen model) which are otherwise lost
    until the wizard's final step.
    """
    if _INSTALL_DIR is None:
        return
    st = load_state()
    st['installed_ids'] = installed_ids()
    # Don't flip setup_complete here. That's the wizard's call.
    save_state(st)


# --- Disk helpers -----------------------------------------------------------

def disk_for(path):
    """Return {'free': bytes, 'total': bytes, 'used': bytes} for path."""
    try:
        u = shutil.disk_usage(str(path))
        return {'free': u.free, 'total': u.total, 'used': u.used}
    except FileNotFoundError:
        # Try parent
        return disk_for(Path(path).parent)


def installed_zim_files():
    """Return set of (kiwix_name, file_path) tuples for ZIMs present on disk."""
    zim_dir = _INSTALL_DIR / 'kiwix' / 'zim'
    if not zim_dir.exists():
        return []
    out = []
    for f in zim_dir.iterdir():
        # Skip macOS AppleDouble sidecars (._filename) created on exFAT.
        if f.name.startswith('._'):
            continue
        if f.suffix == '.zim' and f.stat().st_size > 0:
            out.append((f.stem, str(f)))
    return out


def installed_ids():
    """Return list of curated catalog IDs that are present on disk."""
    files_by_name = {name: path for name, path in installed_zim_files()}
    found = []
    for item in _active_catalog()['items']:
        # ZIM filenames embed a date suffix (e.g. wikipedia_en_all_maxi_2024-09)
        # Match by prefix
        prefix = item['kiwix_name']
        for fname in files_by_name:
            if fname.startswith(prefix):
                found.append(item['id'])
                break
    return found


# --- Download manager -------------------------------------------------------

class Download:
    """Represents one ZIM download (or LLM download)."""
    def __init__(self, item_id, name, url, dest_path, expected_size):
        self.id = item_id
        self.name = name
        self.url = url
        self.dest_path = Path(dest_path)
        self.part_path = self.dest_path.with_suffix(self.dest_path.suffix + '.part')
        self.expected_size = expected_size
        self.downloaded = 0
        self.status = 'queued'  # queued | downloading | done | error | cancelled
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.cancel_flag = threading.Event()
        self._thread = None

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'dest': str(self.dest_path),
            'total': self.expected_size,
            'downloaded': self.downloaded,
            'status': self.status,
            'error': self.error,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.status = 'downloading'
        self.started_at = time.time()
        # Retry network failures with exponential backoff. Multi-GB downloads
        # over hours of wall time WILL hit transient failures (DNS hiccups,
        # WiFi roams, server-side TCP resets). Without retry the user sees a
        # 95%-complete download die at the finish line and has to manually
        # click Retry, which they correctly do not trust.
        MAX_ATTEMPTS = 8
        attempt = 0
        last_error = None

        try:
            self.dest_path.parent.mkdir(parents=True, exist_ok=True)

            while attempt < MAX_ATTEMPTS:
                if self.cancel_flag.is_set():
                    self.status = 'cancelled'
                    return

                # Re-check the partial size on every attempt; the prior
                # attempt may have downloaded MORE bytes before failing.
                existing = 0
                if self.part_path.exists():
                    existing = self.part_path.stat().st_size
                self.downloaded = existing

                # Quick exit if a previous attempt completed the file (e.g.
                # the rename below succeeded but we got here via a bad
                # exception path). Defensive only.
                if self.expected_size and existing >= self.expected_size:
                    break

                attempt += 1
                # Clear any prior error message before the new attempt so
                # the UI stops showing a stale failure during retry.
                self.error = None
                self.status = 'downloading'

                try:
                    req = urllib.request.Request(self.url)
                    if existing > 0:
                        req.add_header('Range', f'bytes={existing}-')

                    with urllib.request.urlopen(req, timeout=60) as resp:
                        cl = resp.headers.get('Content-Length')
                        cr = resp.headers.get('Content-Range')
                        if cr and '/' in cr:
                            try:
                                self.expected_size = int(cr.split('/')[-1])
                            except Exception:
                                pass
                        elif cl and existing == 0:
                            try:
                                self.expected_size = int(cl)
                            except Exception:
                                pass

                        mode = 'ab' if existing > 0 else 'wb'
                        with open(self.part_path, mode) as out:
                            chunk_size = 1024 * 1024
                            while True:
                                if self.cancel_flag.is_set():
                                    self.status = 'cancelled'
                                    return
                                chunk = resp.read(chunk_size)
                                if not chunk:
                                    break
                                out.write(chunk)
                                self.downloaded += len(chunk)

                    # Made it through without exception. Done.
                    last_error = None
                    break

                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                    # Transient network failure. Back off and retry. We
                    # explicitly catch the error classes that cover DNS
                    # failures (URLError [Errno 8]), connection drops,
                    # socket timeouts, and broken pipe writes. Any other
                    # exception type falls through to the outer handler
                    # and ends the download.
                    last_error = f"{type(e).__name__}: {e}"
                    if attempt >= MAX_ATTEMPTS:
                        # Final attempt also failed. Let the outer handler
                        # mark this as a hard error.
                        raise
                    # Exponential backoff capped at 60s. We sleep with the
                    # cancel_flag check so the user can still cancel during
                    # backoff (otherwise a slow retry chain looks frozen).
                    backoff = min(5 * (2 ** (attempt - 1)), 60)
                    self.status = 'retrying'
                    self.error = (
                        f"{last_error}\n"
                        f"Network failure, retrying in {backoff}s "
                        f"(attempt {attempt}/{MAX_ATTEMPTS})"
                    )
                    # Sleep in 1s slices so cancel still feels instant.
                    deadline = time.time() + backoff
                    while time.time() < deadline:
                        if self.cancel_flag.is_set():
                            self.status = 'cancelled'
                            return
                        time.sleep(1.0)

            # Atomic rename. By here the .part file is complete or we'd
            # have raised in the loop above.
            self.part_path.rename(self.dest_path)
            self.status = 'done'
            self.finished_at = time.time()
            self.error = None
            try:
                if self.dest_path.suffix == '.zim':
                    _schedule_shim_reload()
            except Exception:
                pass
            try:
                _persist_state_after_download()
            except Exception:
                pass
        except Exception as e:
            self.status = 'error'
            # Use the last network error if we have one (more useful than
            # a re-raised wrapper). The retry loop sets last_error on each
            # transient failure.
            self.error = last_error or f"{type(e).__name__}: {e}"
            self.finished_at = time.time()

    def cancel(self):
        self.cancel_flag.set()


def _active_catalog():
    """Use hydrated catalog if available, else baked-in. Used by lookup helpers."""
    with _HYDRATION_LOCK:
        if _HYDRATED_CATALOG is not None:
            return _HYDRATED_CATALOG
    return _CATALOG


def _get_item(item_id):
    for it in _active_catalog()['items']:
        if it['id'] == item_id:
            return it
    return None


def start_downloads(ids, model_choice='3b'):
    """Queue and start downloads for the given IDs plus the LLM."""
    started = []
    with _DOWNLOADS_LOCK:
        zim_dir = _INSTALL_DIR / 'kiwix' / 'zim'

        for item_id in ids:
            item = _get_item(item_id)
            if not item:
                continue
            # Already downloaded or in progress?
            if item_id in _DOWNLOADS and _DOWNLOADS[item_id].status in ('downloading', 'queued', 'done'):
                continue

            # Derive filename from URL
            fname = item['download_url'].rsplit('/', 1)[-1]
            dest = zim_dir / fname

            d = Download(item_id, item['title'], item['download_url'], dest, item['size_bytes'])
            _DOWNLOADS[item_id] = d
            d.start()
            started.append(item_id)

        # LLM download
        if model_choice and model_choice != 'none':
            llm_dir = _INSTALL_DIR / 'llm'
            if model_choice == '3b':
                url = 'https://huggingface.co/Mozilla/Llama-3.2-3B-Instruct-llamafile/resolve/main/Llama-3.2-3B-Instruct.Q6_K.llamafile'
                fname = 'Llama-3.2-3B-Instruct.Q6_K.llamafile'
                size = 2_700_000_000
                title = 'Llama 3.2 3B (faster)'
            else:  # 8b
                url = 'https://huggingface.co/Mozilla/Meta-Llama-3.1-8B-Instruct-llamafile/resolve/main/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile'
                fname = 'Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile'
                size = 4_900_000_000
                title = 'Llama 3.1 8B (smarter)'

            dest = llm_dir / fname
            llm_id = f'llm_{model_choice}'
            if llm_id not in _DOWNLOADS or _DOWNLOADS[llm_id].status in ('error', 'cancelled'):
                if not dest.exists():
                    d = Download(llm_id, title, url, dest, size)
                    _DOWNLOADS[llm_id] = d
                    d.start()
                    started.append(llm_id)

    return started


def cancel_download(item_id):
    with _DOWNLOADS_LOCK:
        d = _DOWNLOADS.get(item_id)
        if d:
            d.cancel()
            return True
    return False


def retry_download(item_id):
    """Restart a download that errored out or was cancelled.

    Resume is implicit: Download._run reads any existing .part file size and
    sends a Range header, so the new attempt picks up where the last one
    stopped. We need this for the wizard's per-row Retry button. The only
    other path was 'cancel everything and start over', which loses progress
    on the downloads that succeeded.

    Returns True if a retry was kicked off, False if the id is unknown or
    the download is already running / done.
    """
    with _DOWNLOADS_LOCK:
        d = _DOWNLOADS.get(item_id)
        if not d:
            return False
        # Don't re-spawn an active or completed thread.
        if d.status in ('queued', 'downloading', 'done'):
            return False
        # Reset state so the UI shows a fresh attempt. The .part file on disk
        # stays. Download._run picks it up via the existing-bytes check.
        d.status = 'queued'
        d.error = None
        d.finished_at = None
        d.cancel_flag = threading.Event()
        d.start()
    return True


def clear_download(item_id):
    """Drop a download from the tracker AND delete its partial .part file.

    Used by the wizard's per-row Remove button on errored / cancelled rows.
    For DONE rows the user should use the existing remove_installed flow,
    which deletes the finalized .zim by catalog prefix.

    Returns True if anything was cleared, False if the id is unknown or
    still active. We refuse to clear a running download; caller should
    cancel first, then clear).
    """
    with _DOWNLOADS_LOCK:
        d = _DOWNLOADS.get(item_id)
        if not d:
            return False
        if d.status in ('queued', 'downloading'):
            return False
        # Best-effort delete of the partial. Silently swallow errors; the
        # tracker entry comes off either way so the user isn't soft-locked.
        try:
            if d.part_path.exists():
                d.part_path.unlink()
        except Exception:
            pass
        _DOWNLOADS.pop(item_id, None)
    return True


def remove_installed(item_id):
    """Delete a ZIM file from disk."""
    item = _get_item(item_id)
    if not item:
        return False
    prefix = item['kiwix_name']
    zim_dir = _INSTALL_DIR / 'kiwix' / 'zim'
    removed_any = False
    for f in zim_dir.iterdir():
        # Skip macOS AppleDouble sidecars.
        if f.name.startswith('._'):
            continue
        if f.suffix == '.zim' and f.stem.startswith(prefix):
            try:
                f.unlink()
                removed_any = True
            except Exception:
                pass
    # Drop from in-memory tracker too
    with _DOWNLOADS_LOCK:
        _DOWNLOADS.pop(item_id, None)
    return removed_any


def downloads_snapshot():
    with _DOWNLOADS_LOCK:
        return [d.to_dict() for d in _DOWNLOADS.values()]


# --- HTML pages -------------------------------------------------------------

def _read_version():
    """Read version from VERSION file at the repo/bundle root.

    Resolution order:
      1. <resource_root>/VERSION  (PyInstaller bundle places this at root)
      2. <bin>/../VERSION         (source tree)
      3. fallback string          (build glitch)
    """
    candidates = [
        _resource_root() / 'VERSION',
        Path(__file__).resolve().parent.parent / 'VERSION',
    ]
    for p in candidates:
        try:
            if p.exists():
                v = p.read_text(encoding='utf-8').strip()
                if v:
                    return v
        except Exception:
            continue
    return 'unknown'


def _resource_root():
    """Return the directory that contains bin/templates/, bin/static/, data/.

    Resolution order:
      1. sys._MEIPASS  -> PyInstaller bundle (Win/Linux: bundle root,
         macOS .app: Contents/Resources/ which is where datas land).
      2. <bin>/.. when running from source (repo root).

    On macOS .app bundles, code lives under Contents/Frameworks/bin/ but data
    files (templates, static, catalog.json, Apocalypse.html) live under
    Contents/Resources/. Path(__file__).parent is wrong; sys._MEIPASS is right.
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


_APP_VERSION = _read_version()


def _read_template(name):
    """Load HTML template from the bundle resource root.

    Templates live at <resource_root>/bin/templates/<name>.
    """
    candidates = [
        _resource_root() / 'bin' / 'templates' / name,
        Path(__file__).resolve().parent / 'templates' / name,  # source-mode fallback
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding='utf-8')
    raise FileNotFoundError(
        f"template {name!r} not found in {[str(c) for c in candidates]}"
    )


def _read_landing_page():
    """Load Apocalypse.html (the polished landing page) from the bundle.

    Apocalypse.html is bundled at the resource root (spec line 60).

    Returns None if not found, so callers can fall back to a stub.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        _resource_root() / 'Apocalypse.html',
        here.parent / 'Apocalypse.html',           # source repo root
        here / 'templates' / 'Apocalypse.html',     # last-ditch fallback
    ]
    for p in candidates:
        try:
            if p.exists():
                return p.read_text(encoding='utf-8')
        except Exception:
            continue
    return None


# --- Dispatch ---------------------------------------------------------------

_LOCAL_ADDRS = {'127.0.0.1', '::1', 'localhost'}

def _is_local(client_ip, headers=None):
    """Return True if the request is a direct local connection (not via Cloudflare tunnel).

    cloudflared connects to the shim as 127.0.0.1, so socket IP alone is not
    enough. Cloudflare always injects CF-Connecting-IP on tunnelled requests.
    If that header is present the request is remote even if the socket is local.
    """
    if headers and headers.get('Cf-Connecting-Ip'):
        return False  # came through Cloudflare tunnel = remote visitor
    return (client_ip or '').split(':')[0] in _LOCAL_ADDRS

_ADMIN_PATHS = {
    '/admin', '/setup',
    '/api/download/start', '/api/download/start_url',
    '/api/download/cancel', '/api/download/retry',
    '/api/download/clear', '/api/download/remove',
    '/api/download/progress',
    '/api/config', '/api/setup/complete',
    '/api/services/restart', '/api/install-dir',
    '/api/drives', '/api/browse',
}

def _remote_block():
    """403 response for requests that must stay local."""
    body = b'{"error":"admin access restricted to localhost"}'
    return 403, [('Content-Type', 'application/json')], body


def dispatch_get(path, qs, client_ip=None, headers=None):
    """Returns (status, headers, body_bytes) or None if not handled."""
    if not is_initialized():
        return None

    # Block admin/setup/destructive routes from remote clients
    if path in _ADMIN_PATHS and not _is_local(client_ip, headers):
        return _remote_block()

    if path == '/setup':
        body = _read_template('setup.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/library':
        body = _read_template('library.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/chat':
        body = _read_template('chat.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path.startswith('/static/'):
        # Serve static assets (CSS, JS) from bin/static/. Resolves correctly
        # in source mode AND PyInstaller bundles (including macOS .app where
        # data files land in Contents/Resources/).
        rel = path[len('/static/'):]
        if not rel or '..' in rel.split('/'):
            return 404, [('Content-Type', 'text/plain')], b'not found'
        candidates = [
            _resource_root() / 'bin' / 'static' / rel,
            Path(__file__).resolve().parent / 'static' / rel,  # source-mode fallback
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    data = cand.read_bytes()
                    ct = 'application/octet-stream'
                    if rel.endswith('.css'):
                        ct = 'text/css; charset=utf-8'
                    elif rel.endswith('.js'):
                        ct = 'application/javascript; charset=utf-8'
                    elif rel.endswith('.svg'):
                        ct = 'image/svg+xml'
                    elif rel.endswith('.png'):
                        ct = 'image/png'
                    return 200, [
                        ('Content-Type', ct),
                        ('Cache-Control', 'public, max-age=300'),
                    ], data
            except Exception:
                continue
        return 404, [('Content-Type', 'text/plain')], b'not found'
    if path == '/games' or path == '/games/' or path == '/games/index.html':
        # Bundled games index. Themed via /static/themes.css.
        candidates = [
            _resource_root() / 'games' / 'index.html',
            Path(__file__).resolve().parent.parent / 'games' / 'index.html',
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return 200, [('Content-Type', 'text/html; charset=utf-8')], cand.read_bytes()
            except Exception:
                continue
        return 404, [('Content-Type', 'text/plain')], b'games not bundled'
    if path.startswith('/games/'):
        # Serve any games/*.html. No directory traversal allowed.
        rel = path[len('/games/'):]
        if not rel or '..' in rel.split('/') or '/' in rel:
            return 404, [('Content-Type', 'text/plain')], b'not found'
        candidates = [
            _resource_root() / 'games' / rel,
            Path(__file__).resolve().parent.parent / 'games' / rel,
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    ct = 'text/html; charset=utf-8' if rel.endswith('.html') else 'application/octet-stream'
                    return 200, [
                        ('Content-Type', ct),
                        ('Cache-Control', 'public, max-age=300'),
                    ], cand.read_bytes()
            except Exception:
                continue
        return 404, [('Content-Type', 'text/plain')], b'not found'
    if path == '/' or path == '/index.html':
        # First-run UX: if setup hasn't been completed yet, redirect to the
        # wizard. Otherwise serve the polished Apocalypse.html landing page
        # (terminal theme, search, RAG chat).
        st = load_state()
        if not st.get('setup_complete', False) and not installed_ids():
            return 302, [('Location', '/setup')], b''
        landing = _read_landing_page()
        if landing is None:
            # Bundle didn't ship Apocalypse.html. Fall through to the shim's
            # stub _index() so the user at least sees a list of ZIMs.
            return None
        return 200, [('Content-Type', 'text/html; charset=utf-8')], landing.encode('utf-8')
    if path == '/admin':
        body = _read_template('admin.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/about':
        body = _read_template('about.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/api/catalog':
        force = (qs.get('fresh') or ['0'])[0] in ('1', 'true', 'yes')
        catalog = _serve_catalog(force_fresh=force)
        # Decorate with hydration metadata so UI can show "as of X"
        out = dict(catalog)
        with _HYDRATION_LOCK:
            out['_hydrated_at'] = _HYDRATED_AT
            out['_is_live'] = _HYDRATED_CATALOG is not None
        return _json_response(out)
    if path == '/api/browse':
        if kiwix_opds is None:
            return _json_response({
                'total': 0, 'page': 1, 'page_size': 30, 'total_pages': 0,
                'entries': [], 'error': 'OPDS module unavailable',
            })
        q = (qs.get('q') or [''])[0]
        try:
            page = int((qs.get('page') or ['1'])[0])
        except ValueError:
            page = 1
        try:
            page_size = int((qs.get('page_size') or ['30'])[0])
        except ValueError:
            page_size = 30
        # Annotate results with installed status so UI can grey out installed entries
        result = kiwix_opds.browse(query=q, page=page, page_size=page_size)
        installed_names = {name for name, _ in installed_zim_files()}
        for entry in result['entries']:
            # An entry is installed if any file on disk starts with name+flavour
            prefix_no_flav = entry['name']
            prefix_with_flav = f"{entry['name']}_{entry['flavour']}" if entry['flavour'] else entry['name']
            entry['installed'] = any(
                f.startswith(prefix_with_flav) or f.startswith(prefix_no_flav)
                for f in installed_names
            )
        return _json_response(result)
    if path == '/api/status':
        st = load_state()
        return _json_response({
            'install_dir': str(_INSTALL_DIR),
            'setup_complete': st.get('setup_complete', False),
            'installed_ids': installed_ids(),
            'theme': st.get('theme', 'terminal'),
            'model': st.get('model', '3b'),
            'disk': disk_for(_INSTALL_DIR),
            'version': _APP_VERSION,
            'author': 'Henry Ratterman',
            'author_url': 'https://henryratterman.com',
        })
    if path == '/api/disk':
        target = (qs.get('path') or [str(_INSTALL_DIR)])[0]
        return _json_response(disk_for(target))
    if path == '/api/drives':
        try:
            from drives import list_drives
            return _json_response({'drives': list_drives()})
        except Exception as e:
            return _json_response({'drives': [], 'error': str(e)})
    if path == '/api/install-dir':
        return _json_response({
            'path': str(_INSTALL_DIR),
            'disk': disk_for(_INSTALL_DIR),
        })
    if path == '/api/download/progress':
        return _json_response({
            'downloads': downloads_snapshot(),
            'installed_ids': installed_ids(),
        })
    if path == '/api/config':
        return _json_response(load_state())

    return None


def dispatch_post(path, body, client_ip=None, headers=None):
    if not is_initialized():
        return None

    # Block all POST admin/destructive routes from remote clients
    if path in _ADMIN_PATHS and not _is_local(client_ip, headers):
        return _remote_block()

    if path == '/api/download/start':
        ids = body.get('ids', [])
        model = body.get('model', '3b')
        # Persist the model choice immediately so a crash mid-download
        # doesn't lose it.
        try:
            st = load_state()
            st['model'] = model
            save_state(st)
        except Exception:
            pass
        started = start_downloads(ids, model)
        return _json_response({'started': started})
    if path == '/api/download/start_url':
        # Install a ZIM by direct URL (used by Browse tab for non-curated entries)
        url = body.get('url', '').strip()
        title = body.get('name', '').strip() or url.rsplit('/', 1)[-1]
        try:
            size = int(body.get('size', 0))
        except (TypeError, ValueError):
            size = 0
        if not url or not url.startswith(('http://', 'https://')):
            return _json_response({'started': False, 'error': 'invalid url'})
        # Strip .meta4 if Kiwix gave us the metalink
        if url.endswith('.meta4'):
            url = url[:-len('.meta4')]
        fname = url.rsplit('/', 1)[-1]
        # Synthesize a stable id from filename (without .zim)
        item_id = f"url_{fname.replace('.zim', '')}"
        zim_dir = _INSTALL_DIR / 'kiwix' / 'zim'
        dest = zim_dir / fname
        with _DOWNLOADS_LOCK:
            existing = _DOWNLOADS.get(item_id)
            if existing and existing.status in ('downloading', 'queued', 'done'):
                return _json_response({'started': False, 'reason': 'already running', 'id': item_id})
            d = Download(item_id, title, url, dest, size)
            _DOWNLOADS[item_id] = d
            d.start()
        return _json_response({'started': True, 'id': item_id})
    if path == '/api/download/cancel':
        ok = cancel_download(body.get('id'))
        return _json_response({'cancelled': ok})
    if path == '/api/download/retry':
        ok = retry_download(body.get('id'))
        return _json_response({'retried': ok})
    if path == '/api/download/clear':
        ok = clear_download(body.get('id'))
        return _json_response({'cleared': ok})
    if path == '/api/download/remove':
        ok = remove_installed(body.get('id'))
        return _json_response({'removed': ok})
    if path == '/api/services/restart':
        if _SHIM_RESTART_HOOK:
            _SHIM_RESTART_HOOK()
        return _json_response({'ok': True})
    if path == '/api/install-dir':
        # Set/relocate the install directory. Used by the wizard's drive
        # picker on first run.
        #
        # Constraints: this is safe ONLY when nothing has been downloaded
        # yet (no ZIMs, setup not complete). For an established install,
        # moving the location requires a manual data copy + restart and is
        # not handled here.
        new_path = (body.get('path') or '').strip()
        if not new_path:
            return _json_response({'ok': False, 'error': 'No path provided'})
        # Refuse to relocate if we already have ZIMs at the current location.
        # The user should finish their existing install or wipe state.json first.
        st = load_state()
        if st.get('setup_complete') or installed_ids():
            return _json_response({
                'ok': False,
                'error': 'Cannot relocate after setup. Move the apocalypse folder manually and restart.',
            })
        try:
            from drives import validate_install_path
            ok, reason, info = validate_install_path(new_path)
        except Exception as e:
            return _json_response({'ok': False, 'error': f'Validation failed: {e}'})
        if not ok:
            return _json_response({'ok': False, 'error': reason, 'disk': info})
        # Persist the choice so the next launch picks it up.
        try:
            pointer = Path.home() / '.apocalypse_install'
            pointer.write_text(str(Path(new_path).resolve()), encoding='utf-8')
        except Exception as e:
            return _json_response({'ok': False, 'error': f'Cannot save pointer: {e}'})
        # In-process relocate so the running wizard can keep going without
        # needing a real restart. Updates _INSTALL_DIR + ZIM_DIR + ARCHIVES.
        try:
            relocate_install_dir(new_path)
        except Exception as e:
            return _json_response({'ok': False, 'error': f'Relocate failed: {e}'})
        return _json_response({
            'ok': True, 'path': str(Path(new_path).resolve()),
            'disk': disk_for(_INSTALL_DIR),
        })
    if path == '/api/config':
        st = load_state()
        st.update(body)
        save_state(st)
        return _json_response(st)
    if path == '/api/setup/complete':
        st = load_state()
        st['setup_complete'] = True
        st.update(body)
        save_state(st)
        return _json_response({'ok': True})

    return None


def _json_response(obj):
    body = json.dumps(obj).encode('utf-8')
    return 200, [('Content-Type', 'application/json')], body
