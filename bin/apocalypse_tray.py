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
.app Icon.run() enters NSRunLoop and silently hangs with no logs and no UI.
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
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
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
# write_text() at startup wipes the parent's lines including any FATAL
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
    #
    # CRITICAL: do NOT call .resolve() or .mkdir() here. Both stat() the SD
    # card, and an idle/asleep USB drive at boot will silently throw OSError
    # and fall through to the broken candidate search. We trust the pointer
    # absolutely. If the drive is genuinely missing, downstream code (find_model,
    # state.json read) will handle it gracefully and the self-heal loop will
    # recover when the drive wakes up.
    pointer = Path.home() / '.apocalypse_install'
    try:
        if pointer.exists():
            target = pointer.read_text(encoding='utf-8').strip()
            if target:
                p = Path(target).expanduser()
                _llog(f"find_install_dir: using pointer file -> {p}")
                # Best-effort mkdir but DO NOT block on it. If the SD is
                # asleep, the parent dir already exists from the wizard's
                # earlier write of state.json. mkdir failure is informational.
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except (OSError, PermissionError) as e:
                    _llog(f"find_install_dir: pointer mkdir non-fatal: {e}")
                return p
        else:
            _llog("find_install_dir: no pointer file present")
    except Exception as e:
        _llog(f"find_install_dir: pointer read failed: {e}")

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

    def _is_usable_dir(path, timeout=8.0):
        """Stronger check: directory exists AND we can listdir() it.
        Catches drives where stat() works but readdir() is wedged (fskit bug).

        Retries once after a brief sleep to give an idle/sleeping USB drive
        a chance to spin up. Drives that are truly wedged stay wedged across
        retries; drives that are just asleep usually wake within ~2 seconds.
        """
        if not _exists_with_timeout(path, 2.0):
            return False
        for attempt in range(2):
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
            if not t.is_alive() and result[0]:
                return True
            if attempt == 0:
                # Give the drive a moment to wake up and try once more
                time.sleep(0.5)
        return False

    candidates = []
    rejected_by_usable_check = []  # paths where outer dir check failed,
                                    # but state.json may still exist
    if sys.platform == 'darwin' and _exists_with_timeout(Path('/Volumes'), 1.0):
        try:
            volumes = os.listdir('/Volumes')
        except OSError:
            volumes = []
        for d in volumes:
            c = Path('/Volumes') / d / 'apocalypse'
            # Use the stronger usability check for external volumes.
            if _is_usable_dir(c, 8.0):
                candidates.append(c)
            else:
                _llog(f"_is_usable_dir failed for /Volumes/{d}/apocalypse, will retry by state.json")
                rejected_by_usable_check.append(c)
    candidates += [
        Path.home() / 'Apocalypse',
        Path.home() / 'apocalypse',
        Path.home() / 'Documents' / 'Apocalypse',
    ]
    if sys.platform == 'win32':
        candidates.append(Path('C:/Apocalypse'))

    # Pass 1: prefer any candidate (or rejected candidate) that has an existing
    # state.json. state.json existence is the strongest signal that this IS
    # the user's real install. Probing state.json directly side-steps the
    # listdir wedge bug where an idle USB takes longer than the timeout.
    for c in list(candidates) + rejected_by_usable_check:
        if c and _exists_with_timeout(c / 'state.json', 3.0):
            _llog(f"found existing install with state.json at {c}")
            return c

    # Pass 2: first usable candidate (no prior state, fresh install).
    for c in candidates:
        if c and _exists_with_timeout(c, 1.0):
            _llog(f"using first usable candidate (no prior state) at {c}")
            return c

    # Default: ~/Apocalypse (created on first run)
    default = Path.home() / 'Apocalypse'
    default.mkdir(parents=True, exist_ok=True)
    _llog(f"no candidates found, defaulting to {default}")
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
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            # install_dir may point to a stale/missing drive (e.g. pointer file
            # still says /Volumes/Untitled but drive is now /Volumes/ApocaDrive 1).
            # Fall back to a temp log so the app can still start and show the
            # setup wizard, rather than crashing on launch.
            _llog(f"ShimManager: log dir mkdir failed ({e}), falling back to temp log")
            self.log_path = Path(tempfile.gettempdir()) / 'apocalypse_shim.log'

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

    def _check_relocation(self):
        """If the wizard wrote a new ~/.apocalypse_install pointer, re-target.

        Called each find_model() so the tray follows the wizard's drive picker
        without requiring a restart. Without this, the tray keeps looking at
        the boot-time install_dir even after the wizard relocated to a USB.

        CRITICAL: do NOT call .resolve() on the pointer target. Same reason
        as find_install_dir() resolve() stat()s the SD card and a sleeping
        USB drive will silently throw OSError. Compare paths as strings.
        """
        pointer = Path.home() / '.apocalypse_install'
        if not pointer.exists():
            return
        try:
            target_str = pointer.read_text(encoding='utf-8').strip()
        except Exception:
            return
        if not target_str:
            return
        target = Path(target_str).expanduser()
        # Compare without resolve() to avoid waking the SD card unnecessarily.
        if str(target) == str(self.install_dir):
            return
        # Pointer changed since boot, re-target.
        # Don't gate on target.exists() that's also a stat() that wedges.
        # If the dir is genuinely gone, find_model() will return None below
        # and we just keep retrying every tick. No harm.
        self.install_dir = target
        self.llm_dir = target / 'llm'
        # NB: do NOT rewire log_path. Keeps the existing log continuous.

    def find_model(self):
        """Pick the best available .llamafile (prefer 8B if RAM >= 12 GB).

        Returns Path or None if no model is installed.
        """
        # Re-check the install pointer on every call. This lets the tray
        # follow the wizard's drive picker without a restart.
        self._check_relocation()
        if not self.llm_dir.exists():
            return None
        # Filter out macOS AppleDouble sidecars (._filename). These are HFS
        # metadata stubs created when copying to exFAT/FAT32. They match
        # *.llamafile globs but are tiny binary blobs that crash /bin/sh
        # with "cannot execute binary file" if we try to run them.
        candidates = sorted(
            f for f in (list(self.llm_dir.glob('*.llamafile')) +
                        list(self.llm_dir.glob('*.gguf')))
            if not f.name.startswith('._')
        )
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
    """Generate a 64x64 menu bar icon: ASCII block 'A' colored by status.

    Transparent background so the macOS menu bar shows through. Color
    encodes service health (green=ok, yellow=downloading, red=down,
    gray=starting). Same block-glyph as the app icon and the homepage
    banner so the brand stays consistent across surfaces.
    """
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    colors = {
        'gray':   (140, 140, 140, 255),
        'green':  (74, 222, 128, 255),
        'yellow': (251, 191, 36, 255),
        'red':    (248, 113, 113, 255),
        'orange': (255, 169, 64, 255),
    }
    fg = colors.get(color, colors['gray'])

    # Same block-A as bin/make_icons.py, kept inline so the tray module
    # has no import dependency on it.
    glyph = [
        "   █████╗   ",
        "  ██╔══██╗  ",
        "  ███████║  ",
        "  ██╔══██║  ",
        "  ██║  ██║  ",
        "  ╚═╝  ╚═╝  ",
    ]

    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        "C:\\Windows\\Fonts\\consola.ttf",
        "C:\\Windows\\Fonts\\lucon.ttf",
    ]
    font_path = next((p for p in candidates if os.path.exists(p)), None)

    rows = len(glyph)
    block = "\n".join(glyph)

    target_h = int(size * 0.78)
    font_size = max(6, int(target_h / rows))

    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    bbox = d.multiline_textbbox((0, 0), block, font=font, spacing=0)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if bw > size * 0.92 and font_path:
        font_size = max(6, int(font_size * (size * 0.92) / bw))
        font = ImageFont.truetype(font_path, font_size)
        bbox = d.multiline_textbbox((0, 0), block, font=font, spacing=0)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (size - bw) // 2 - bbox[0]
    y = (size - bh) // 2 - bbox[1]
    d.multiline_text((x, y), block, font=font, fill=fg, spacing=0)
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

                # Self-heal SHIM: respawn if subprocess died. Without this,
                # menu bar icon can show stale "alive" state while ECONNREFUSED
                # on the actual port.
                if not self.shim.is_running():
                    _llog("health_loop: shim died, respawning")
                    threading.Thread(target=self.shim.start, daemon=True).start()

                # Self-heal LLM: if a llamafile appears on disk later (e.g.
                # downloaded via wizard after boot), start it. Idempotent
                # via is_running() short-circuit.
                if not self.llama.is_running() and self.llama.find_model():
                    threading.Thread(target=self.llama.start, daemon=True).start()
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
            # Verify the icon file is reachable and well-formed BEFORE handing
            # it to rumps. _nsimage_from_file inside rumps just calls
            # NSImage.initByReferencingFile_ which silently returns nil on
            # bad input, leaving an empty menu-bar slot with no error.
            initial_icon = self._icon_paths['yellow']
            try:
                with open(initial_icon, 'rb') as fp:
                    head = fp.read(8)
                if not head.startswith(b'\x89PNG'):
                    _llog(f"WARN: icon file at {initial_icon} is not a valid PNG: head={head!r}")
                    initial_icon = None
                else:
                    _llog(f"icon ok: {initial_icon}")
            except OSError as e:
                _llog(f"WARN: cannot read icon file {initial_icon}: {e}")
                initial_icon = None

            # Always pass a non-empty title even when the icon loads. If the
            # icon ever fails to render (Cocoa silently drops bad NSImage),
            # the title text guarantees the menu-bar slot is visible. Without
            # this, the user sees NOTHING in the menu bar and assumes the app
            # didn't launch which is exactly what happened on Henry's
            # MacBook with v1.3.3.
            super().__init__(
                'Apocalypse',
                title='⚪︎',          # always-visible fallback
                icon=initial_icon,
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
                # Map each health state to (color, glyph). The glyph stays
                # visible even if the icon image fails to render (rumps'
                # NSImage falls back to empty when the path is bad see
                # icon-validation in __init__).
                state_map = {
                    'down':         ('red',    '✕'),
                    'starting':     ('yellow', '⚪︎'),
                    'ready':        ('green',  '●'),
                    'downloading':  ('orange', '↓'),
                }
                color, glyph = state_map.get(health, ('gray', '⚪︎'))
                if health != self.core.last_health:
                    self.core.last_health = health
                    try:
                        self.icon = self._icon_paths[color]
                    except Exception as e:
                        _llog(f"icon swap failed: {e}")
                    self.title = glyph
                    self._build_menu()

                # First-run auto-open of the wizard
                if not self.core._first_check_done and health == 'ready':
                    self.core._first_check_done = True
                    if not self.core.shim.setup_complete():
                        webbrowser.open(self.core.url('/setup'))
                    else:
                        # Already set up open main page so user lands somewhere useful
                        webbrowser.open(self.core.url('/'))

                # Self-heal SHIM: if the shim subprocess died (port 8888 dead),
                # respawn it. Covers the case where the user pkill'd the shim
                # but not the tray, or the shim crashed mid-session. Without
                # this, the menu bar can stay visually "alive" with green icon
                # cached state while the wizard/landing page returns ECONNREFUSED.
                if not self.core.shim.is_running():
                    _llog("tick: shim died, respawning")
                    threading.Thread(target=self.core.shim.start, daemon=True).start()

                # Self-heal LLM: if a llamafile is on disk but the process
                # isn't running, start it. Covers the common case where the
                # user downloaded the model AFTER app boot via the wizard;
                # the initial llama.start() at __main__ ran with no model
                # present and exited cleanly. This re-checks every tick and
                # is idempotent (is_running() short-circuits when alive).
                if not self.core.llama.is_running() and self.core.llama.find_model():
                    threading.Thread(target=self.core.llama.start, daemon=True).start()
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

    # Self-heal: if this binary is running from inside an apocalypse-drive
    # folder on an external drive, update the pointer file to match. This
    # fixes the "pointer still says /Volumes/Untitled" bug when the drive
    # was renamed or remounted with a different suffix.
    try:
        exe_path = Path(sys.argv[0]).resolve()
        # .app on macOS: .../apocalypse-drive/macos/Apocalypse.app/Contents/MacOS/Apocalypse
        # Look for the apocalypse data dir relative to the .app location
        for parent in exe_path.parents:
            candidate = parent.parent / 'apocalypse'
            if (candidate / 'state.json').exists() or (candidate / 'kiwix').exists():
                pointer = Path.home() / '.apocalypse_install'
                current = pointer.read_text(encoding='utf-8').strip() if pointer.exists() else ''
                if str(candidate) != current:
                    pointer.write_text(str(candidate) + '\n', encoding='utf-8')
                    _llog(f"self-healed pointer: {current!r} -> {candidate}")
                break
    except Exception as e:
        _llog(f"self-heal pointer: skipped ({e})")

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
        # When launched from a terminal (running from source), re-raise so
        # the dev sees the traceback. When launched as a bundled .app there's
        # no stdout, raising would just abort with no user feedback. The log
        # file is the user feedback. Best-effort cleanup of the shim and
        # llama subprocesses so they don't outlive the parent and squat on
        # ports forever.
        try:
            if app is not None:
                app.shim.stop()
                app.llama.stop()
        except Exception:
            pass
        if not hasattr(sys, '_MEIPASS'):
            raise
        # Bundled .app: exit cleanly so launchd/Finder don't show a crash dialog
        sys.exit(1)
