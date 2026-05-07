"""
Setup wizard + admin endpoints for the Apocalypse drive.

Plugs into kiwix_shim.py via dispatch_setup() / dispatch_setup_post().

Routes:
  GET  /setup                  -> setup wizard HTML (first-run)
  GET  /admin                  -> admin/library management HTML
  GET  /api/catalog            -> JSON catalog (curated ZIMs + bundles)
  GET  /api/status             -> JSON system status (installed ZIMs, downloads, services)
  GET  /api/disk?path=<p>      -> JSON {free, total} for given path
  POST /api/download/start     -> start downloads {ids: [...], dest_dir: "...", model: "3b"|"8b"|"none"}
  GET  /api/download/progress  -> JSON list of {id, name, total, downloaded, status}
  POST /api/download/cancel    -> cancel a running download {id: "..."}
  POST /api/download/remove    -> delete an installed ZIM {id: "..."} (frees disk)
  POST /api/services/restart   -> restart kiwix-serve / shim to pick up new ZIMs
  GET  /api/config             -> JSON current config (theme, model, install_dir, etc.)
  POST /api/config             -> update config

State lives in <install_dir>/state.json. Downloads run in background threads
using urllib (no external deps). Resume via Range header.
"""
import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# --- Module state -----------------------------------------------------------

_INSTALL_DIR = None        # Path: where the apocalypse drive lives
_CATALOG = None            # dict: parsed catalog.json
_DOWNLOADS = {}            # id -> Download object
_DOWNLOADS_LOCK = threading.Lock()
_SHIM_RESTART_HOOK = None  # callable: triggers shim to reload ZIMs


def init(install_dir, catalog_path, restart_hook=None):
    """Call once at shim startup."""
    global _INSTALL_DIR, _CATALOG, _SHIM_RESTART_HOOK
    _INSTALL_DIR = Path(install_dir).resolve()
    _SHIM_RESTART_HOOK = restart_hook
    with open(catalog_path) as f:
        _CATALOG = json.load(f)
    # Ensure dirs exist
    (_INSTALL_DIR / 'kiwix' / 'zim').mkdir(parents=True, exist_ok=True)
    (_INSTALL_DIR / 'llm').mkdir(parents=True, exist_ok=True)
    (_INSTALL_DIR / 'logs').mkdir(parents=True, exist_ok=True)


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
        if f.suffix == '.zim' and f.stat().st_size > 0:
            out.append((f.stem, str(f)))
    return out


def installed_ids():
    """Return list of curated catalog IDs that are present on disk."""
    files_by_name = {name: path for name, path in installed_zim_files()}
    found = []
    for item in _CATALOG['items']:
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
        try:
            # Create parent dir
            self.dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Resume support via Range header
            existing = 0
            if self.part_path.exists():
                existing = self.part_path.stat().st_size
                self.downloaded = existing

            req = urllib.request.Request(self.url)
            if existing > 0:
                req.add_header('Range', f'bytes={existing}-')

            with urllib.request.urlopen(req, timeout=60) as resp:
                # Capture true total from Content-Range or Content-Length
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
                    chunk_size = 1024 * 1024  # 1 MB
                    while True:
                        if self.cancel_flag.is_set():
                            self.status = 'cancelled'
                            return
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        self.downloaded += len(chunk)

            # Atomic rename
            self.part_path.rename(self.dest_path)
            self.status = 'done'
            self.finished_at = time.time()
        except Exception as e:
            self.status = 'error'
            self.error = f"{type(e).__name__}: {e}"
            self.finished_at = time.time()

    def cancel(self):
        self.cancel_flag.set()


def _get_item(item_id):
    for it in _CATALOG['items']:
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


def remove_installed(item_id):
    """Delete a ZIM file from disk."""
    item = _get_item(item_id)
    if not item:
        return False
    prefix = item['kiwix_name']
    zim_dir = _INSTALL_DIR / 'kiwix' / 'zim'
    removed_any = False
    for f in zim_dir.iterdir():
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

def _read_template(name):
    """Load HTML template from same dir as this module."""
    here = Path(__file__).parent
    p = here / 'templates' / name
    return p.read_text(encoding='utf-8')


# --- Dispatch ---------------------------------------------------------------

def dispatch_get(path, qs):
    """Returns (status, headers, body_bytes) or None if not handled."""
    if not is_initialized():
        return None

    if path == '/setup':
        body = _read_template('setup.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/admin':
        body = _read_template('admin.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/about':
        body = _read_template('about.html').encode('utf-8')
        return 200, [('Content-Type', 'text/html; charset=utf-8')], body
    if path == '/api/catalog':
        return _json_response(_CATALOG)
    if path == '/api/status':
        st = load_state()
        return _json_response({
            'install_dir': str(_INSTALL_DIR),
            'setup_complete': st.get('setup_complete', False),
            'installed_ids': installed_ids(),
            'theme': st.get('theme', 'terminal'),
            'model': st.get('model', '3b'),
            'disk': disk_for(_INSTALL_DIR),
            'version': '1.0.0',
            'author': 'Henry Ratterman',
            'author_url': 'https://henryratterman.com',
        })
    if path == '/api/disk':
        target = (qs.get('path') or [str(_INSTALL_DIR)])[0]
        return _json_response(disk_for(target))
    if path == '/api/download/progress':
        return _json_response({
            'downloads': downloads_snapshot(),
            'installed_ids': installed_ids(),
        })
    if path == '/api/config':
        return _json_response(load_state())

    return None


def dispatch_post(path, body):
    if not is_initialized():
        return None

    if path == '/api/download/start':
        ids = body.get('ids', [])
        model = body.get('model', '3b')
        started = start_downloads(ids, model)
        return _json_response({'started': started})
    if path == '/api/download/cancel':
        ok = cancel_download(body.get('id'))
        return _json_response({'cancelled': ok})
    if path == '/api/download/remove':
        ok = remove_installed(body.get('id'))
        return _json_response({'removed': ok})
    if path == '/api/services/restart':
        if _SHIM_RESTART_HOOK:
            _SHIM_RESTART_HOOK()
        return _json_response({'ok': True})
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
