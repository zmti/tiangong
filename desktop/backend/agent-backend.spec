# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Use venv's Python, not system/miniconda
venv_site = r'E:\dev\agent-service\.venv\Lib\site-packages'

def _collect_dir(src):
    out = []
    if not os.path.isdir(src):
        return out
    for f in os.listdir(src):
        p = os.path.join(src, f)
        if os.path.isfile(p):
            out.append((p, '.'))
    return out

app_datas, app_binaries, app_hiddenimports = collect_all('app')
uvicorn_datas, uvicorn_binaries, uvicorn_hiddenimports = collect_all('uvicorn')

# conda 系 Python 的 C 运行库放在 <prefix>/Library/bin,PyInstaller 不会自动收集,
# 冻结后依次报 "_ctypes/_sqlite3/_lzma/... DLL load failed"。按名单缺啥补啥。
import sys
_lib_bin = os.path.join(sys.base_prefix, 'Library', 'bin')
for _name in (
    'ffi.dll',            # _ctypes
    'sqlite3.dll',        # _sqlite3
    'liblzma.dll',        # _lzma
    'libbz2.dll',         # _bz2(大小写两种命名都试)
    'LIBBZ2.dll',
    'zlib.dll',           # _hashlib / _zlib
    'libexpat.dll',       # pyexpat / _elementtree
    'libcrypto-3-x64.dll',  # _ssl
    'libssl-3-x64.dll',
    'libmpdec-4.dll',     # _decimal
):
    _src = os.path.join(_lib_bin, _name)
    if os.path.isfile(_src):
        app_binaries.append((_src, '.'))
_ffi_dlls = os.path.join(sys.base_prefix, 'DLLs', 'ffi.dll')
if os.path.isfile(_ffi_dlls):
    app_binaries.append((_ffi_dlls, '.'))

a = Analysis(
    [r'E:\dev\agent-service\desktop\backend\launcher.py'],
    pathex=[r'E:\dev\agent-service', venv_site],
    binaries=app_binaries + uvicorn_binaries,
    datas=app_datas + uvicorn_datas,
    hiddenimports=[
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'edge_tts',
        'numpy',
        'matplotlib',
        'matplotlib.backends.backend_agg',
    'websockets',
    'websockets.exceptions',
    'croniter',
    'croniter.croniter',
  ] + app_hiddenimports + uvicorn_hiddenimports,
    hookspath=[r'E:\dev\agent-service\desktop\backend\hooks'],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'scipy', 'pandas', 'IPython', 'notebook',
        'PIL.AvifImagePlugin', 'PIL._avif',
        'PIL.DdsImagePlugin', 'PIL.HeifImagePlugin',
        'matplotlib.backends.backend_qt5', 'matplotlib.backends.backend_qt4',
        'matplotlib.backends.backend_gtk', 'matplotlib.backends.backend_gtk3',
        'matplotlib.backends.backend_tkagg', 'matplotlib.backends.backend_wx',
        'matplotlib.backends.backend_wxagg',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

_PIL_SKIP = ('avif', 'heif', 'dds', 'gifstream')
filtered = []
for tup in a.binaries:
    name = os.path.basename(tup[0]).lower()
    if 'pil' in os.path.dirname(tup[0]).lower().replace(os.sep, '/') and name.endswith('.pyd') and any(k in name for k in _PIL_SKIP):
        continue
    filtered.append(tup)
a.binaries = filtered

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='agent-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='agent-backend',
)
