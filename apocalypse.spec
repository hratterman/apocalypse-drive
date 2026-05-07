# PyInstaller spec for Apocalypse menu-bar app, cross-platform.
#
# Run:  pyinstaller apocalypse.spec
#
# Outputs:
#   macOS:   dist/Apocalypse.app
#   Windows: dist/Apocalypse/Apocalypse.exe
#   Linux:   dist/Apocalypse/Apocalypse
#
# What's bundled:
#   - Python interpreter
#   - apocalypse_tray.py (entry point)
#   - kiwix_shim.py + setup_routes.py (the backend)
#   - bin/templates/*.html (setup wizard, admin)
#   - data/catalog.json (the curated ZIM catalog)
#   - assets/icon.* (icons)
#   - libzim, pystray, Pillow (deps)
#
# What's NOT bundled:
#   - The actual ZIM files (downloaded by user via wizard)
#   - The llamafile (downloaded by user via wizard)
#   - kiwix-tools binaries (we don't use them; the shim replaces them)

import os
import sys
import site
from pathlib import Path

block_cipher = None
ROOT = Path(os.path.abspath(SPECPATH if 'SPECPATH' in dir() else '.'))

# Locate libzim's .so/.pyd and its bundled native library
def _find_libzim():
    """Returns list of (src, dst_dir) tuples for libzim files."""
    out = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        sp_path = Path(sp)
        if not sp_path.exists():
            continue
        # Top-level .so / .pyd extension module (varies by platform)
        for pattern in ('libzim*.so', 'libzim*.pyd', 'libzim*.dylib'):
            for f in sp_path.glob(pattern):
                out.append((str(f), '.'))
        # libzim/ package containing libzim.X.dylib (macOS) or .so (Linux)
        pkg = sp_path / 'libzim'
        if pkg.exists():
            for f in pkg.iterdir():
                if f.suffix in ('.dylib', '.so', '.dll', '.pyi', '.pxd'):
                    out.append((str(f), 'libzim'))
    return out

# Files to embed inside the bundle
datas = [
    (str(ROOT / 'bin' / 'kiwix_shim.py'),     'bin'),
    (str(ROOT / 'bin' / 'setup_routes.py'),   'bin'),
    (str(ROOT / 'bin' / 'kiwix_opds.py'),     'bin'),
    (str(ROOT / 'bin' / 'templates'),         'bin/templates'),
    (str(ROOT / 'bin' / 'static'),            'bin/static'),
    (str(ROOT / 'data' / 'catalog.json'),     'data'),
    (str(ROOT / 'Apocalypse.html'),           '.'),
]

binaries = _find_libzim()
print(f"[apocalypse.spec] libzim binaries: {len(binaries)}")
for b in binaries:
    print(f"  {b[0]} -> {b[1]}")

# Hidden imports: rumps (macOS) / pystray (Win+Linux) / PIL backends + libzim runtime
hiddenimports = [
    'libzim',
    'libzim.reader',
    'libzim.search',
    'libzim.suggestion',
    'uuid',                # libzim imports this at init time
    'datetime',
    'setup_routes',        # imported dynamically by kiwix_shim
    'kiwix_opds',          # imported dynamically by setup_routes
    'certifi',             # SSL cert bundle for HTTPS downloads
    'ssl',
    'PIL._tkinter_finder',
]

if sys.platform == 'darwin':
    # rumps + its PyObjC deps. PyInstaller's pyobjc hook usually catches the
    # frameworks, but we explicitly list rumps' top-level dependencies so a
    # cold cache build doesn't drop them.
    hiddenimports += [
        'rumps',
        'AppKit',
        'Foundation',
        'objc',
        'PyObjCTools.AppHelper',
    ]
else:
    # pystray's per-platform backends. PyInstaller can't see these because
    # pystray imports them dynamically based on sys.platform.
    hiddenimports += [
        'pystray._win32',         # Windows
        'pystray._gtk',           # Linux GTK
        'pystray._appindicator',  # Linux GNOME
        'pystray._xorg',          # Linux X11 fallback
    ]

a = Analysis(
    [str(ROOT / 'bin' / 'apocalypse_tray.py')],
    pathex=[str(ROOT / 'bin')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'IPython', 'jupyter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Per-platform icon
if sys.platform == 'darwin':
    icon_path = str(ROOT / 'assets' / 'icon.icns')
elif sys.platform == 'win32':
    icon_path = str(ROOT / 'assets' / 'icon.ico')
else:
    icon_path = str(ROOT / 'assets' / 'icon-512.png')

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Apocalypse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # No terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # Unsigned (Henry's choice, Plan A in v1)
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Apocalypse',
)

# macOS .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Apocalypse.app',
        icon=icon_path,
        bundle_identifier='com.hratterman.apocalypse',
        version='1.0.0',
        info_plist={
            'CFBundleName': 'Apocalypse',
            'CFBundleDisplayName': 'Apocalypse',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'NSHighResolutionCapable': True,
            # LSUIElement: True makes the app menu-bar-only — no Dock icon,
            # no Cmd+Tab entry, no Force Quit menu entry. Sounds clean, but
            # if the menu-bar icon ever fails to render (which has bitten us
            # on a fresh MacBook in v1.3.3), the app becomes unreachable
            # without Activity Monitor. Keep the app accessible.
            'LSUIElement': False,
            'LSMinimumSystemVersion': '10.13',
            'NSHumanReadableCopyright': 'MIT License · github.com/hratterman/apocalypse-drive',
            'NSAppTransportSecurity': {
                # Allow plain HTTP to localhost (the shim) and HTTPS to anywhere
                'NSAllowsLocalNetworking': True,
            },
        },
    )
