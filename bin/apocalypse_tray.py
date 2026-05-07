"""
Apocalypse menu-bar / system-tray app.

What it does:
  - Spawns the kiwix_shim.py background process
  - Lives in the menu bar (macOS) / system tray (Win/Linux)
  - Click icon -> menu with: Open Apocalypse, Open Library, Setup Wizard, Restart Service, View Logs, Quit
  - Detects first-run state (no ZIMs installed) and auto-opens the setup wizard in the browser
  - Polls the shim every 5s to update the icon (green=ok, yellow=downloading, red=down)

Dependencies:
  pystray >= 0.19    (cross-platform tray)
  Pillow             (icon rendering)

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
    import pystray
    from pystray import MenuItem, Menu
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: pystray and Pillow required. Install with: pip install pystray Pillow", file=sys.stderr)
    sys.exit(1)


# ---- Path resolution -------------------------------------------------------

def find_install_dir():
    """Find the apocalypse install dir (where ZIMs/LLMs/state live).

    Search order:
      1. APOCALYPSE_DIR env var
      2. The directory containing this script's parent (bin/.. == install_dir)
         (only if that dir has actual data: kiwix/zim/ or state.json)
      3. Common locations: /Volumes/*/apocalypse, ~/Apocalypse, etc.
      4. Default: ~/Apocalypse (created on first run)
    """
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

    # Search common paths
    candidates = []
    if sys.platform == 'darwin' and Path('/Volumes').exists():
        for d in os.listdir('/Volumes'):
            c = Path('/Volumes') / d / 'apocalypse'
            if c.exists():
                candidates.append(c)
    candidates += [
        Path.home() / 'Apocalypse',
        Path.home() / 'apocalypse',
        Path.home() / 'Documents' / 'Apocalypse',
    ]
    if sys.platform == 'win32':
        candidates.append(Path('C:/Apocalypse'))

    for c in candidates:
        if c and c.exists():
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


# ---- Main app --------------------------------------------------------------

class ApocalypseApp:
    def __init__(self):
        self.install_dir = find_install_dir()
        self.code_dir = find_code_dir()
        port = int(os.environ.get('APOCALYPSE_PORT', '8888'))
        self.shim = ShimManager(self.install_dir, self.code_dir, port=port)
        self.icon = None
        self.last_health = None

        # Belt-and-suspenders cleanup: no matter how this process dies (menu
        # Quit, Force Quit, parent crash, SIGTERM from launchd), the child
        # shim must die too. Without this, the child holds files in the .app
        # bundle and Finder refuses to trash the app.
        atexit.register(self.shim.stop)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, self._signal_exit)
            except (ValueError, OSError, AttributeError):
                # Some signals aren't available on Windows / non-main threads.
                pass

    def _signal_exit(self, signum, frame):
        try:
            self.shim.stop()
        finally:
            # Re-raise default behavior so the process actually exits.
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
        # Start the shim
        self.shim.start()

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


if __name__ == '__main__':
    # PyInstaller-aware multitool dispatch:
    # When the bundled .app spawns a subprocess via sys.executable, that subprocess
    # IS the bundle launcher (not a separate Python). If we just point it at
    # kiwix_shim.py, it would re-launch the whole tray app instead. So we use
    # a sentinel arg --run-shim to mean "execute the shim module instead of the
    # tray UI". The tray's ShimManager.start() passes --run-shim followed by
    # the rest of the shim args.
    if len(sys.argv) > 1 and sys.argv[1] == '--run-shim':
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

    app = ApocalypseApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit_app()
