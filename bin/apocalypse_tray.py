"""
Apocalypse menu-bar / system-tray app.

What it does:
  - Spawns the kiwix_shim.py background process
  - Lives in the menu bar (macOS) / system tray (Win/Linux)
  - Click icon -> menu with: Open Apocalypse, Open Library, Setup Wizard, Restart Service, View Logs, Quit
  - Detects first-run state (no ZIMs installed) and auto-opens the setup wizard in the browser
  - Polls the shim every 5s to update the icon (green=ok, yellow=downloading, red=down)

Dependencies:
  macOS:           rumps >= 0.4         (native menu-bar app via PyObjC)
  Windows/Linux:   pystray >= 0.19      (cross-platform tray)
  All platforms:   Pillow               (icon rendering)

Why two libraries: pystray's macOS backend is unreliable inside a PyInstaller
.app — Icon.run() enters NSRunLoop and silently hangs with no logs and no UI.
rumps is the standard macOS-native menu-bar library used by countless shipped
.app bundles, and its rumps.quit_application() actually exits the process.

When packaged via PyInstaller, both ship inside the .app/.exe.

Layout (relative to install_dir):
  install_dir/
    bin/
      apocalypse_tray.py      (this file)
      kiwix_shim.py
      setup_routes.py
      templates/
    data/
      catalog.json
    kiwix/zim/                (downloaded ZIMs go here)
    llm/                      (downloaded llamafiles go here)
    logs/
      shim.log
"""
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: Pillow required. Install with: pip install Pillow", file=sys.stderr)
    sys.exit(1)

# Platform-specific menu-bar / tray library
_USE_RUMPS = sys.platform == 'darwin'
if _USE_RUMPS:
    try:
        import rumps
    except ImportError:
        print("ERROR: rumps required on macOS. Install with: pip install rumps", file=sys.stderr)
        sys.exit(1)
else:
    try:
        import pystray
        from pystray import MenuItem, Menu
    except ImportError:
        print("ERROR: pystray required. Install with: pip install pystray", file=sys.stderr)
        sys.exit(1)


# ---- Early launch log ------------------------------------------------------
# Bundled .app on macOS has no stdout/stderr visible to the user. If the app
# hangs or crashes during startup (e.g. a wedged USB drive blocking stat() on
# /Volumes), there's no way to diagnose without this log. Write progress to a
# known location starting from line 1.
#
# Critical detail: when the parent tray spawns its shim subprocess via
# sys.executable inside a PyInstaller bundle, the child re-runs THIS module
# with --run-shim. If both processes share one log file, the child's
# write_text() at startup wipes the parent's lines — including any FATAL
# traceback. So the child writes to a separate file.
_IS_SHIM_CHILD = len(sys.argv) > 1 and sys.argv[1] == '--run-shim'
if _IS_SHIM_CHILD:
    _LAUNCH_LOG_PATH = Path.home() / '.apocalypse_shim_launch.log'
else:
    _LAUNCH_LOG_PATH = Path.home() / '.apocalypse_launch.log'

def _llog(msg):
    try:
        with open(_LAUNCH_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            f.flush()
    except Exception:
        pass

# Truncate previous log on each launch so it stays readable
try:
    _LAUNCH_LOG_PATH.write_text(f"=== Apocalypse launch {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} role={'shim-child' if _IS_SHIM_CHILD else 'tray-parent'} ===\n", encoding='utf-8')
except Exception:
    pass
_llog(f"argv={sys.argv} platform={sys.platform} python={sys.version.split()[0]}")


# ---- Path resolution -------------------------------------------------------

def find_install_dir():
    """Find the apocalypse install dir (where ZIMs/LLMs/state live).

    Search order:
      0. ~/.apocalypse_install pointer file (set by the wizard's drive picker)
      1. APOCALYPSE_DIR env var
      2. The directory containing this script's parent (bin/.. == install_dir)
         (only if that dir has actual data: kiwix/zim/ or state.json)
      3. Common locations: /Volumes/*/apocalypse, ~/Apocalypse, etc.
      4. Default: ~/Apocalypse (created on first run)
    """
    # 0. Pointer file written by the wizard's drive picker. This is THE way
    # users tell us "put my data on the external drive" without env vars.
    pointer = Path.home() / '.apocalypse_install'
    if pointer.exists():
        try:
            target = pointer.read_text(encoding='utf-8').strip()
            if target:
                p = Path(target).expanduser().resolve()
                # Be tolerant: if the drive isn't mounted, fall through to
                # the rest of the search instead of crashing.
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    return p
                except (OSError, PermissionError):
                    pass
        except Exception:
            pass

    env = os.environ.get('APOCALYPSE_DIR')
    if env:
        p = Path(env).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # If this file is at <install>/bin/apocalypse_tray.py and that install dir
    # has actual data, use it.
    here = Path(__file__).resolve().parent
    candidate = here.parent
    if (candidate / 'kiwix' / 'zim').exists() or (candidate / 'state.json').exists():
        return candidate

    # Search common paths.
    # NOTE: stat()ing /Volumes/<drive>/apocalypse can hang indefinitely if a
    # mounted drive is in a wedged state (fskit/exFAT bugs on macOS, stale NFS
    # mounts, USB drives that went to sleep). We protect every probe with a
    # short thread-based timeout so a single bad volume cannot freeze the app
    # at launch with zero error message.
    def _exists_with_timeout(path, timeout=1.0):
        result = [False]
        def _probe():
            try:
                result[0] = path.exists()
            except Exception:
                result[0] = False
        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return result[0] if not t.is_alive() else False

    def _is_usable_dir(path, timeout=2.0):
        """Stronger check: directory exists AND we can listdir() it.
        Catches drives where stat() works but readdir() is wedged (fskit bug).
        """
        if not _exists_with_timeout(path, 1.0):
            return False
        result = [False]
        def _probe():
            try:
                os.listdir(str(path))
                result[0] = True
            except Exception:
                result[0] = False
        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return result[0] if not t.is_alive() else False

    candidates = []
    if sys.platform == 'darwin' and _exists_with_timeout(Path('/Volumes'), 1.0):
        try:
            volumes = os.listdir('/Volumes')
        except OSError:
            volumes = []
        for d in volumes:
            c = Path('/Volumes') / d / 'apocalypse'
            # Use the stronger usability check for external volumes.
            if _is_usable_dir(c, 2.0):
                candidates.append(c)
            else:
                _llog(f"skipping wedged or missing /Volumes/{d}/apocalypse")
    candidates += [
        Path.home() / 'Apocalypse',
        Path.home() / 'apocalypse',
        Path.home() / 'Documents' / 'Apocalypse',
    ]
    if sys.platform == 'win32':
        candidates.append(Path('C:/Apocalypse'))

    for c in candidates:
        if c and _exists_with_timeout(c, 1.0):
            return c

    # Default: ~/Apocalypse (created on first run)
    default = Path.home() / 'Apocalypse'
    default.mkdir(parents=True, exist_ok=True)
    return default


def find_code_dir():
    """Find the directory containing kiwix_shim.py and templates.

    This is where the *code* lives, separate from where the user's ZIMs live.
    When run from source, code_dir == this script's parent (bin/).
    When run from a PyInstaller bundle, code_dir == sys._MEIPASS/bin/.
    """
    # PyInstaller temp dir
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / 'bin'
    return Path(__file__).resolve().parent


# ---- Shim process management ----------------------------------------------

class ShimManager:
    def __init__(self, install_dir, code_dir, port=8888):
        self.install_dir = Path(install_dir)
        self.code_dir = Path(code_dir)
        self.port = port
        self.process = None
        self.log_path = self.install_dir / 'logs' / 'shim.log'
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def health(self):
        """Returns one of: 'down', 'starting', 'ready', 'downloading'."""
        if not self.is_running():
            return 'down'
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/status', timeout=1.5) as r:
                if r.status != 200:
                    return 'starting'
                data = json.loads(r.read())
                # If any downloads are in progress, signal that
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/download/progress', timeout=1.0) as r2:
                        prog = json.loads(r2.read())
                        active = [d for d in prog.get('downloads', []) if d.get('status') in ('downloading', 'queued')]
                        if active:
                            return 'downloading'
                except Exception:
                    pass
                return 'ready'
        except Exception:
            return 'starting'

    def setup_complete(self):
        """Has the user finished the setup wizard?"""
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/status', timeout=1.5) as r:
                data = json.loads(r.read())
                return data.get('setup_complete', False) and len(data.get('installed_ids', [])) > 0
        except Exception:
            return False

    def start(self):
        if self.is_running():
            return
        log_fp = open(self.log_path, 'a')
        log_fp.write(f"\n=== Tray app started shim at {time.ctime()} ===\n")
        log_fp.flush()

        py = sys.executable
        is_frozen = hasattr(sys, '_MEIPASS')

        if is_frozen:
            # PyInstaller bundle: re-invoke ourselves with --run-shim sentinel.
            # The bundled launcher will dispatch to kiwix_shim.main() instead of
            # the tray UI.
            cmd = [py, '--run-shim',
                   '--install-dir', str(self.install_dir),
                   '--port', str(self.port),
                   '--host', '127.0.0.1']
            # Catalog is bundled inside _MEIPASS/data/
            catalog_path = Path(sys._MEIPASS) / 'data' / 'catalog.json'
        else:
            # Run-from-source: invoke the shim script directly.
            shim_path = self.code_dir / 'kiwix_shim.py'
            if not shim_path.exists():
                print(f"shim not found at {shim_path}", file=sys.stderr)
                return
            cmd = [py, str(shim_path),
                   '--install-dir', str(self.install_dir),
                   '--port', str(self.port),
                   '--host', '127.0.0.1']
            catalog_path = self.code_dir.parent / 'data' / 'catalog.json'

        if catalog_path.exists():
            cmd += ['--catalog', str(catalog_path)]

        kwargs = {'stdout': log_fp, 'stderr': subprocess.STDOUT, 'cwd': str(self.install_dir)}
        if sys.platform == 'win32':
            # Group on Windows so we can taskkill the whole tree.
            kwargs['creationflags'] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Put the child in its own process group on POSIX so we can
            # signal the whole tree at once (and so the child doesn't share
            # our controlling terminal).
            kwargs['start_new_session'] = True
        self.process = subprocess.Popen(cmd, **kwargs)

    def stop(self):
        proc = self.process
        if not proc or proc.poll() is not None:
            self.process = None
            return
        try:
            if sys.platform == 'win32':
                # Send Ctrl+Break to the process group, then taskkill /T /F as
                # a hammer fallback.
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    subprocess.call(
                        ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                # SIGTERM the entire process group, then SIGKILL if needed.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            # Last resort: don't let stop() raise during atexit.
            try:
                proc.kill()
            except Exception:
                pass
        self.process = None

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()


# ---- LLM server management -------------------------------------------------

class LlamaServerManager:
    """Run a .llamafile from <install_dir>/llm/ as a child process on port 8081.

    The shim makes RAG calls to LLAMAFILE_URL (default http://127.0.0.1:8081).
    Without this manager nothing ever starts the .llamafile, so the LLM
    feature is dead even after the user downloads the model. We mirror
    ShimManager's process-group hygiene so the LLM dies when the tray dies.
    """

    PORT = 8081

    def __init__(self, install_dir):
        self.install_dir = Path(install_dir)
        self.llm_dir = self.install_dir / 'llm'
        self.process = None
        self.model_path = None
        self.log_path = self.install_dir / 'logs' / 'llamafile.log'
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def find_model(self):
        """Pick the best available .llamafile (prefer 8B if RAM >= 12 GB).

        Returns Path or None if no model is installed.
        """
        if not self.llm_dir.exists():
            return None
        candidates = sorted(self.llm_dir.glob('*.llamafile')) + \
                     sorted(self.llm_dir.glob('*.gguf'))
        if not candidates:
            return None
        # Heuristic: pick the largest model the system can probably run.
        # >=12 GB RAM -> any. <12 GB -> avoid 8B, prefer 3B.
        try:
            import psutil  # noqa: optional
            total_gb = psutil.virtual_memory().total / (1024 ** 3)
        except ImportError:
            total_gb = 16  # assume capable if we can't check
        if total_gb < 12:
            small = [c for c in candidates if '3B' in c.name or '3b' in c.name]
            if small:
                return small[0]
        # Prefer 8B if present and we have RAM.
        big = [c for c in candidates if '8B' in c.name or '8b' in c.name]
        if big and total_gb >= 12:
            return big[0]
        return candidates[0]

    def is_running(self):
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.PORT}/v1/models', timeout=1.0
            ) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self):
        """Launch the .llamafile if a model is installed and one isn't running."""
        if self.is_running():
            return
        if self.process is not None and self.process.poll() is None:
            return
        model = self.find_model()
        if model is None:
            return  # No model installed yet; user can download via wizard.
        self.model_path = model

        # Make the .llamafile executable on POSIX (a fresh download is 0644).
        if sys.platform != 'win32':
            try:
                os.chmod(model, 0o755)
            except OSError:
                pass

        # Build command. llamafiles are self-extracting on macOS / Linux. On
        # Windows they need to be renamed to .exe to run; we copy/rename
        # lazily on first start.
        if sys.platform == 'win32':
            exe_path = model.with_suffix('.exe')
            if not exe_path.exists():
                try:
                    import shutil as _sh
                    _sh.copy2(model, exe_path)
                except Exception:
                    pass
            cmd = [str(exe_path)]
        else:
            cmd = ['/bin/sh', str(model)]

        cmd += [
            '--server', '--nobrowser',
            '--host', '127.0.0.1',
            '--port', str(self.PORT),
            '-c', '4096',                # context length
            '-ngl', '999',               # offload all layers to GPU if available
        ]

        log_fp = open(self.log_path, 'a', buffering=1)
        log_fp.write(f"\n=== llamafile start: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_fp.write(f"model: {model}\n")
        log_fp.write(f"cmd: {' '.join(cmd)}\n")
        log_fp.flush()

        kwargs = {
            'stdout': log_fp, 'stderr': subprocess.STDOUT,
            'cwd': str(self.install_dir),
        }
        if sys.platform == 'win32':
            kwargs['creationflags'] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs['start_new_session'] = True
        try:
            self.process = subprocess.Popen(cmd, **kwargs)
        except (FileNotFoundError, PermissionError, OSError) as e:
            log_fp.write(f"FAILED to spawn llamafile: {e}\n")
            self.process = None

    def stop(self):
        proc = self.process
        if not proc or proc.poll() is not None:
            self.process = None
            return
        try:
            if sys.platform == 'win32':
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    subprocess.call(
                        ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.process = None

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()


# ---- Tray icon -------------------------------------------------------------

def make_icon(color='gray'):
    """Generate a 64x64 icon with a colored dot."""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Outer ring
    d.ellipse((8, 8, 56, 56), outline=(180, 180, 180, 255), width=2)
    # Inner dot
    colors = {
        'gray':       (140, 140, 140, 255),
        'green':      (74, 222, 128, 255),
        'yellow':     (251, 191, 36, 255),
        'red':        (248, 113, 113, 255),
        'orange':     (255, 169, 64, 255),
    }
    fill = colors.get(color, colors['gray'])
    d.ellipse((20, 20, 44, 44), fill=fill)
    return img


def write_icon_png(color, dest_dir):
    """Write a colored icon PNG to dest_dir and return its absolute path.

    rumps takes an icon path (not an in-memory PIL image), so we materialize
    each color variant once at startup and reuse the path on icon updates.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / f"apocalypse_{color}.png"
    if not p.exists():
        make_icon(color).save(p)
    return str(p)


# ---- Main app --------------------------------------------------------------

class ApocalypseApp:
    def __init__(self):
        self.install_dir = find_install_dir()
        self.code_dir = find_code_dir()
        port = int(os.environ.get('APOCALYPSE_PORT', '8888'))
        self.shim = ShimManager(self.install_dir, self.code_dir, port=port)
        self.llama = LlamaServerManager(self.install_dir)
        self.icon = None
        self.last_health = None
        # Used by ApocalypseRumpsApp's tick handler to fire the wizard auto-open
        # exactly once. The pystray path uses its own first_check local in
        # health_loop, so this attribute is harmless on Win/Linux.
        self._first_check_done = False

        # Belt-and-suspenders cleanup: no matter how this process dies (menu
        # Quit, Force Quit, parent crash, SIGTERM from launchd), all child
        # processes (shim AND llamafile) must die too. Without this, they
        # hold files in the .app bundle and Finder refuses to trash the app.
        atexit.register(self.llama.stop)
        atexit.register(self.shim.stop)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, self._signal_exit)
            except (ValueError, OSError, AttributeError):
                # Some signals aren't available on Windows / non-main threads.
                pass

    def _signal_exit(self, signum, frame):
        try:
            self.llama.stop()
            self.shim.stop()
        finally:
            sys.exit(0)

    def url(self, path=''):
        return f'http://127.0.0.1:{self.shim.port}{path}'

    # Menu actions
    def open_main(self, icon=None, item=None):
        webbrowser.open(self.url('/'))

    def open_library(self, icon=None, item=None):
        webbrowser.open(self.url('/admin'))

    def open_setup(self, icon=None, item=None):
        webbrowser.open(self.url('/setup'))

    def open_about(self, icon=None, item=None):
        webbrowser.open(self.url('/about'))

    def open_website(self, icon=None, item=None):
        webbrowser.open('https://henryratterman.com')

    def open_github(self, icon=None, item=None):
        webbrowser.open('https://github.com/hratterman/apocalypse-drive')

    def restart_service(self, icon=None, item=None):
        self.shim.restart()
        time.sleep(1.0)
        self.update_icon()

    def show_logs(self, icon=None, item=None):
        # Open the log file in the default text editor
        log = self.shim.log_path
        if sys.platform == 'darwin':
            subprocess.call(['open', str(log)])
        elif sys.platform == 'win32':
            os.startfile(str(log))
        else:
            subprocess.call(['xdg-open', str(log)])

    def show_install_dir(self, icon=None, item=None):
        if sys.platform == 'darwin':
            subprocess.call(['open', str(self.install_dir)])
        elif sys.platform == 'win32':
            os.startfile(str(self.install_dir))
        else:
            subprocess.call(['xdg-open', str(self.install_dir)])

    def quit_app(self, icon=None, item=None):
        self.llama.stop()
        self.shim.stop()
        if self.icon:
            self.icon.stop()

    def build_menu(self):
        return Menu(
            MenuItem(f'Apocalypse · {self.shim.health()}', None, enabled=False),
            Menu.SEPARATOR,
            MenuItem('Open Apocalypse', self.open_main, default=True),
            MenuItem('Library', self.open_library),
            MenuItem('Setup Wizard', self.open_setup),
            Menu.SEPARATOR,
            MenuItem('Restart Service', self.restart_service),
            MenuItem('View Logs', self.show_logs),
            MenuItem('Show Install Folder', self.show_install_dir),
            Menu.SEPARATOR,
            MenuItem('About Apocalypse', self.open_about),
            MenuItem('henryratterman.com', self.open_website),
            MenuItem('GitHub Repo', self.open_github),
            Menu.SEPARATOR,
            MenuItem('Quit', self.quit_app),
        )

    def update_icon(self):
        h = self.shim.health()
        if h == self.last_health:
            return
        self.last_health = h
        color_map = {
            'down': 'red',
            'starting': 'yellow',
            'ready': 'green',
            'downloading': 'orange',
        }
        if self.icon:
            self.icon.icon = make_icon(color_map.get(h, 'gray'))
            self.icon.menu = self.build_menu()
            self.icon.title = f'Apocalypse · {h}'

    def health_loop(self):
        # Wait for shim to come up, then auto-open setup if first-run
        time.sleep(2.0)
        first_check = True
        while True:
            try:
                self.update_icon()
                if first_check and self.shim.health() == 'ready':
                    first_check = False
                    if not self.shim.setup_complete():
                        # Auto-open the wizard on first run
                        webbrowser.open(self.url('/setup'))
            except Exception:
                pass
            time.sleep(5.0)

    def run(self):
        # NB: shim and llama are now started by __main__ before this is called
        # (so the same startup path works for both rumps and pystray UIs).
        # ShimManager.start() short-circuits if already running, so this is a
        # safe no-op if the caller already spun things up.
        self.shim.start()
        threading.Thread(target=self.llama.start, daemon=True).start()

        # Build the tray icon
        self.icon = pystray.Icon(
            'apocalypse',
            make_icon('yellow'),
            'Apocalypse · starting',
            menu=self.build_menu(),
        )

        # Background thread to poll health
        t = threading.Thread(target=self.health_loop, daemon=True)
        t.start()

        # This blocks until quit
        self.icon.run()


# ---- macOS rumps wrapper ---------------------------------------------------
# rumps.App must run on the main thread and uses Cocoa's NSRunLoop. It does
# NOT play well with pystray's icon API, but it's the only reliable path on
# macOS. We compose ApocalypseApp for the shared shim/menu-action logic and
# wrap it in a rumps.App subclass for the UI.

if _USE_RUMPS:
    class ApocalypseRumpsApp(rumps.App):
        def __init__(self, core):
            self.core = core  # ApocalypseApp instance (shim, llama, actions)
            # Pre-render every color variant once. rumps takes a path, not a
            # PIL image, so we materialize PNGs in the install dir's logs/
            # subfolder (writable on every platform we care about).
            icon_dir = self.core.install_dir / 'logs' / 'icons'
            self._icon_paths = {
                c: write_icon_png(c, icon_dir)
                for c in ('gray', 'green', 'yellow', 'red', 'orange')
            }
            super().__init__(
                'Apocalypse',
                title=None,
                icon=self._icon_paths['yellow'],
                template=False,         # full-color icon, not B&W template
                quit_button=None,       # we install our own Quit
            )
            self._build_menu()

        # Wrap each core action in a rumps callback signature
        def _wrap(self, fn):
            def _cb(_sender):
                try:
                    fn()
                except Exception as e:
                    _llog(f"menu action error: {type(e).__name__}: {e}")
            return _cb

        def _build_menu(self):
            self.menu.clear()
            health = self.core.shim.health()
            self.menu = [
                rumps.MenuItem(f'Status: {health}', callback=None),
                None,  # separator
                rumps.MenuItem('Open Apocalypse', callback=self._wrap(lambda: self.core.open_main())),
                rumps.MenuItem('Library',         callback=self._wrap(lambda: self.core.open_library())),
                rumps.MenuItem('Setup Wizard',    callback=self._wrap(lambda: self.core.open_setup())),
                None,
                rumps.MenuItem('Restart Service',     callback=self._wrap(lambda: self.core.restart_service())),
                rumps.MenuItem('View Logs',           callback=self._wrap(lambda: self.core.show_logs())),
                rumps.MenuItem('Show Install Folder', callback=self._wrap(lambda: self.core.show_install_dir())),
                None,
                rumps.MenuItem('About Apocalypse', callback=self._wrap(lambda: self.core.open_about())),
                rumps.MenuItem('henryratterman.com', callback=self._wrap(lambda: self.core.open_website())),
                rumps.MenuItem('GitHub Repo', callback=self._wrap(lambda: self.core.open_github())),
                None,
                rumps.MenuItem('Quit', callback=self._on_quit),
            ]

        def _on_quit(self, _sender):
            try:
                self.core.llama.stop()
                self.core.shim.stop()
            finally:
                # rumps.quit_application() actually exits the NSRunLoop.
                # We don't trust pystray-style icon.stop() here because it's
                # exactly what was hanging on Henry's MacBook.
                rumps.quit_application()

        # rumps' built-in repeating timer. Must use this rather than a raw
        # thread because UI updates need to come from the main thread.
        @rumps.timer(5)
        def _tick(self, _sender):
            try:
                health = self.core.shim.health()
                color_map = {
                    'down': 'red',
                    'starting': 'yellow',
                    'ready': 'green',
                    'downloading': 'orange',
                }
                desired = color_map.get(health, 'gray')
                if health != self.core.last_health:
                    self.core.last_health = health
                    self.icon = self._icon_paths[desired]
                    self._build_menu()

                # First-run auto-open of the wizard
                if not self.core._first_check_done and health == 'ready':
                    self.core._first_check_done = True
                    if not self.core.shim.setup_complete():
                        webbrowser.open(self.core.url('/setup'))
                    else:
                        # Already set up — open main page so user lands somewhere useful
                        webbrowser.open(self.core.url('/'))
            except Exception as e:
                _llog(f"tick error: {type(e).__name__}: {e}")


if __name__ == '__main__':
    _llog("entered __main__")
    # PyInstaller-aware multitool dispatch:
    # When the bundled .app spawns a subprocess via sys.executable, that subprocess
    # IS the bundle launcher (not a separate Python). If we just point it at
    # kiwix_shim.py, it would re-launch the whole tray app instead. So we use
    # a sentinel arg --run-shim to mean "execute the shim module instead of the
    # tray UI". The tray's ShimManager.start() passes --run-shim followed by
    # the rest of the shim args.
    if len(sys.argv) > 1 and sys.argv[1] == '--run-shim':
        _llog("dispatching to kiwix_shim.main()")
        # Replace argv with the rest, then exec the shim's main()
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        # Find and exec kiwix_shim
        if hasattr(sys, '_MEIPASS'):
            shim_dir = os.path.join(sys._MEIPASS, 'bin')
        else:
            shim_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, shim_dir)
        import kiwix_shim
        kiwix_shim.main()
        sys.exit(0)

    _llog("constructing ApocalypseApp")
    app = None
    rumps_app = None
    try:
        app = ApocalypseApp()
        _llog(f"ApocalypseApp constructed, install_dir={app.install_dir} code_dir={app.code_dir}")

        # Start the shim (and LLM in the background) before any UI runs. The
        # rumps NSRunLoop blocks the main thread, so spinning these up early
        # is essential.
        _llog("starting shim subprocess")
        app.shim.start()
        _llog(f"shim pid={getattr(app.shim.process, 'pid', None)}")
        threading.Thread(target=app.llama.start, daemon=True).start()

        if _USE_RUMPS:
            _llog("constructing ApocalypseRumpsApp (macOS)")
            rumps_app = ApocalypseRumpsApp(app)
            _llog("ApocalypseRumpsApp ready, entering rumps run loop")
            rumps_app.run()
            _llog("rumps run loop exited")
        else:
            _llog("entering pystray run loop (Win/Linux)")
            app.run()
            _llog("pystray run loop exited")
    except KeyboardInterrupt:
        if app is not None:
            app.quit_app()
    except Exception as e:
        import traceback
        _llog(f"FATAL: {type(e).__name__}: {e}")
        _llog(traceback.format_exc())
        # Re-raise only if we still have stdio (running from terminal). When
        # launched as a bundled .app there's no stdout, but raising would
        # cause Python's default handler to abort with no user feedback. The
        # log file is the user feedback.
        raise
