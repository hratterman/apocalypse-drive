"""
Drive detection for the Apocalypse setup wizard.

Returns a list of mounted volumes the user might want to install onto.
Each entry has:
  - path: absolute mount path
  - label: human-friendly name
  - type: 'internal' | 'external' | 'home' | 'custom'
  - filesystem: best-effort fs type (exfat, apfs, ntfs, ext4, ...)
  - free, total, used: bytes
  - writable: whether we can create files there
  - portable: True if filesystem is cross-OS friendly (exfat / fat32)
  - apocalypse_dir: the path we'd actually install into (e.g. /Volumes/MyDrive/apocalypse)
  - has_existing: True if an apocalypse install already lives there

Cross-platform: macOS (/Volumes), Linux (/media, /mnt, /run/media), Windows
(drive letters via os.popen('wmic') or psutil if available).
"""

from __future__ import annotations
import os
import platform
import shutil
import string
import subprocess
import sys
from pathlib import Path


def _disk(path: Path):
    """Disk usage with hard timeout. Stalled NFS mounts can hang shutil.disk_usage."""
    import threading
    result = {'free': 0, 'total': 0, 'used': 0, '_timeout': False}

    def worker():
        try:
            st = shutil.disk_usage(str(path))
            result['free'] = st.free
            result['total'] = st.total
            result['used'] = st.used
        except Exception:
            pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=2.0)
    if t.is_alive():
        result['_timeout'] = True
    return result


def _writable(path: Path) -> bool:
    """Quick write test by creating and removing a probe file. Bounded by timeout."""
    import threading
    result = [False]

    def worker():
        try:
            if not path.exists() or not path.is_dir():
                return
            probe = path / '.apocalypse_write_probe'
            probe.write_text('ok')
            probe.unlink()
            result[0] = True
        except Exception:
            pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=2.0)
    return result[0]


def _mac_filesystem(mount: Path) -> str:
    """Best-effort fs type via `diskutil info`."""
    try:
        out = subprocess.run(
            ['diskutil', 'info', str(mount)],
            capture_output=True, text=True, timeout=3
        )
        for line in out.stdout.splitlines():
            ll = line.strip().lower()
            if ll.startswith('file system personality:') or ll.startswith('type (bundle):'):
                val = line.split(':', 1)[1].strip().lower()
                # Normalise common values
                if 'exfat' in val:
                    return 'exfat'
                if 'msdos' in val or 'fat32' in val or 'fat' in val:
                    return 'fat32'
                if 'apfs' in val:
                    return 'apfs'
                if 'hfs' in val or 'mac os extended' in val:
                    return 'hfs+'
                if 'ntfs' in val:
                    return 'ntfs'
                return val.split()[0] if val else 'unknown'
    except Exception:
        pass
    return 'unknown'


def _linux_filesystem(mount: Path) -> str:
    """Read /proc/mounts on Linux."""
    try:
        with open('/proc/mounts') as f:
            best = None
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == str(mount):
                    best = parts[2].lower()
                    break
            if best:
                return best
    except Exception:
        pass
    return 'unknown'


def _windows_filesystem(letter: str) -> str:
    """Use ctypes GetVolumeInformation on Windows."""
    if platform.system() != 'Windows':
        return 'unknown'
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        fs_buf = ctypes.create_unicode_buffer(256)
        kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(letter + '\\'),
            None, 0, None, None, None, fs_buf, 256
        )
        return (fs_buf.value or 'unknown').lower()
    except Exception:
        return 'unknown'


PORTABLE_FS = {'exfat', 'fat32', 'vfat', 'msdos', 'fat'}


def _entry(path: Path, label: str, kind: str) -> dict:
    """Build a single drive entry dict."""
    p = path
    fs = 'unknown'
    sysname = platform.system()
    if sysname == 'Darwin':
        fs = _mac_filesystem(p)
    elif sysname == 'Linux':
        fs = _linux_filesystem(p)
    elif sysname == 'Windows':
        # On Windows the path comes in as e.g. 'D:\'
        letter = str(p).rstrip('\\/')
        fs = _windows_filesystem(letter)

    apoc_dir = p / 'apocalypse'
    has_existing = (apoc_dir / 'state.json').exists() or (apoc_dir / 'kiwix' / 'zim').exists()

    # Drive is "writable" if either the root or an existing apocalypse subdir
    # accepts a probe file. Some external drives (especially exFAT mounted
    # by macOS) refuse writes at root but allow them inside subfolders.
    writable = _writable(p)
    if not writable and apoc_dir.exists():
        writable = _writable(apoc_dir)

    return {
        'path': str(p),
        'label': label,
        'type': kind,
        'filesystem': fs,
        'portable': fs in PORTABLE_FS,
        'writable': writable,
        **{k: v for k, v in _disk(p).items() if k != '_timeout'},
        'apocalypse_dir': str(apoc_dir),
        'has_existing': has_existing,
    }


def _list_macos() -> list[dict]:
    out = []
    # Boot volume = home dir's drive. Use ~/Apocalypse as the suggested target
    # because writing to / requires root.
    home = Path.home()
    out.append(_entry(home, f"Home folder ({home.name})", 'home'))

    # External / extra mounts under /Volumes
    volumes = Path('/Volumes')
    if volumes.exists():
        for v in sorted(volumes.iterdir()):
            try:
                if not v.is_dir():
                    continue
                # Skip the boot volume mirror
                if v.name in ('Macintosh HD', 'Macintosh HD - Data'):
                    continue
                # Skip system snapshots and update mounts
                if v.name.startswith('com.apple.') or v.name == 'Recovery':
                    continue
                out.append(_entry(v, v.name, 'external'))
            except Exception:
                continue
    return out


def _list_linux() -> list[dict]:
    out = []
    home = Path.home()
    out.append(_entry(home, f"Home folder ({home.name})", 'home'))

    user = os.environ.get('USER', '')
    candidates = [
        Path('/media') / user if user else None,
        Path('/media'),
        Path('/run/media') / user if user else None,
        Path('/mnt'),
    ]
    seen = set()
    for base in candidates:
        if not base or not base.exists():
            continue
        try:
            for v in sorted(base.iterdir()):
                if not v.is_dir():
                    continue
                rp = v.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                out.append(_entry(v, v.name, 'external'))
        except Exception:
            continue
    return out


def _list_windows() -> list[dict]:
    out = []
    home = Path.home()
    out.append(_entry(home, f"User folder ({home.name})", 'home'))

    # Walk drive letters A-Z, keep ones that exist and have free space.
    home_drive = str(home)[:2].upper()  # e.g. 'C:'
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if not root.exists():
                continue
        except Exception:
            continue
        # Skip the home drive (already covered by home folder entry)
        if f"{letter}:" == home_drive:
            continue
        try:
            label = letter + ':'
            # Try to get the volume label
            if platform.system() == 'Windows':
                try:
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    name_buf = ctypes.create_unicode_buffer(256)
                    kernel32.GetVolumeInformationW(
                        ctypes.c_wchar_p(str(root)),
                        name_buf, 256, None, None, None, None, 0
                    )
                    if name_buf.value:
                        label = f"{name_buf.value} ({letter}:)"
                except Exception:
                    pass
            out.append(_entry(root, label, 'external'))
        except Exception:
            continue
    return out


def list_drives() -> list[dict]:
    """Return all candidate install drives for the current OS."""
    sysname = platform.system()
    try:
        if sysname == 'Darwin':
            return _list_macos()
        if sysname == 'Linux':
            return _list_linux()
        if sysname == 'Windows':
            return _list_windows()
    except Exception as e:
        # Don't crash the wizard on weird mounts. Return at least home.
        pass
    home = Path.home()
    return [_entry(home, f"Home folder ({home.name})", 'home')]


def validate_install_path(p: str):
    """Validate a custom install path. Returns (ok, reason, info) tuple.

    info contains free/total/used bytes for the parent dir when ok=True or when
    the parent at least exists. The ok=False path may have stale info from a
    partial check, so callers must gate on ok.
    """
    try:
        path = Path(p).expanduser().resolve()
    except Exception as e:
        return False, f'invalid path: {e}', {}

    parent = path if path.exists() else path.parent
    if not parent.exists():
        return False, f'parent directory does not exist: {parent}', {}
    if not _writable(parent):
        return False, f'directory is not writable: {parent}', {**{k: v for k, v in _disk(parent).items() if k != '_timeout'}}

    return True, '', {
        'path': str(path),
        'parent': str(parent),
        **{k: v for k, v in _disk(parent).items() if k != '_timeout'},
    }


def validate_path(p: str) -> dict:
    """Back-compat: dict shape used by older callers."""
    ok, reason, info = validate_install_path(p)
    if not ok:
        return {'ok': False, 'reason': reason}
    return {'ok': True, **info}


if __name__ == '__main__':
    import json
    print(json.dumps({'drives': list_drives()}, indent=2))
