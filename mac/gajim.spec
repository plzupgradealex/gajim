# -*- mode: python -*-

block_cipher = None

cwd = os.getcwd()
icon = os.path.join(cwd, "mac", "Gajim.icns")

info_plist = {
    "CFBundleDisplayName": "Gajim",
    "NSHighResolutionCapable": True,
    "CFBundleURLTypes": [{"CFBundleURLName": "XMPP URI",
                          "CFBundleURLSchemes": ["xmpp"]}],
    "NSUIElement": True
}

import sys
import glob
import platform

# Hidden libs to add because we remove PIL._imagingft to avoid non-system versions
hidden_libs = ['libsoup-*.dylib', 'libgtksourceview-*.dylib', 'libspelling-*.dylib']

# Get homebrew lib path according to system arch
if platform.machine() == 'x86_64':
	lib_path = '/usr/local/lib/'
elif platform.machine() == 'arm64':
	lib_path = '/opt/homebrew/lib/'

# Collect every match for each pattern — a typelib can reference a specific
# soname (e.g. libspelling-1.2.dylib), so ship all variants rather than only
# the last glob match (which used to drop the soname the typelib asks for).
hidden_binaries = []
for lib_name in hidden_libs:
	for lib_file in glob.glob(lib_path + lib_name):
		hidden_binaries.append((lib_file, '.'))

# Collect GI-repository typelibs
gi_typelib_files = glob.glob(lib_path + 'girepository-*/*.typelib')
# Skip the GTK3 stack (Gtk-3.0/Gdk-3.0): Gajim is GTK4-only, and shipping
# both makes the GObject type system register GtkWidget twice, corrupting
# GTK and tearing the window down mid-run.
gi_typelib_files = [
    f for f in gi_typelib_files
    if not os.path.basename(f).endswith(
        ('Gtk-3.0.typelib', 'Gdk-3.0.typelib', 'GdkX11-3.0.typelib'))
]
for lib_file in gi_typelib_files:
	hidden_binaries.append((lib_file, 'gi_typelibs/'))

# A Homebrew lib ships as a versioned file plus an API-level symlink that
# both resolve to one file under Cellar (e.g. libspelling-1.2.dylib and
# libspelling-1.dylib). PyInstaller flattens symlinks, so collecting both
# ships two identical copies, and each registers its GObject types on load
# corrupting the type system ("cannot register existing type 'SpellingChecker'").
# Collapse to one entry per real file.
_seen = set()
hidden_binaries = [
	b for b in hidden_binaries
	if os.path.realpath(b[0]) not in _seen and not _seen.add(os.path.realpath(b[0]))
]

sys.path.insert(0, os.path.join(cwd))

modules = glob.glob("gajim/common/modules/*.py")
modules_list = [os.path.basename(f)[:-3] for f in modules if not f.endswith("__init__.py")]
hiddenimports = ['gajim.common.modules.' + m for m in modules_list]

sys.path.pop(0)

a = Analysis(['launch.py'],
             pathex=[cwd],
             binaries=hidden_binaries,
             datas=[('gajim', 'gajim')],
             hiddenimports=hiddenimports,
             hookspath=[],
             runtime_hooks=[],
             excludes=['PIL._imagingft'],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
# Even with the GTK3 typelibs excluded above, PyInstaller's gi hook can still
# drag libgtk-3/libgdk-3 in via another typelib's recorded dependency.
# Nothing in the bundle links them except each other, and Gajim imports
# Gtk-4.0 only, so drop every GTK3 survivor: shipping GTK3 and GTK4 together
# corrupts the GObject type system.
_gtk3_dylibs = ('libgtk-3', 'libgdk-3')
_gtk3_typelibs = ('Gtk-3.0.typelib', 'Gdk-3.0.typelib', 'GdkX11-3.0.typelib')
a.binaries = [
    b for b in a.binaries
    if not os.path.basename(b[0]).startswith(_gtk3_dylibs)
    and os.path.basename(b[0]) not in _gtk3_typelibs
]
a.datas = [d for d in a.datas if os.path.basename(d[0]) not in _gtk3_typelibs]

pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          [],
          exclude_binaries=True,
          name='launch',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          console=False )
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=False,
               upx=True,
               name='launch')
app = BUNDLE(coll,
             name='Gajim.app',
             icon=icon,
             info_plist=info_plist,
             bundle_identifier='org.gajim.gajim')
