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
        'matplotlib',
        'matplotlib.backends.backend_agg',
        'matplotlib.backends.backend_pdf',
        'matplotlib.backends.backend_svg',
        'numpy',
    ] + app_hiddenimports + uvicorn_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'scipy', 'pandas', 'IPython', 'notebook',
        'PIL.AvifImagePlugin', 'PIL._avif',
        'PIL.DdsImagePlugin', 'PIL.HeifImagePlugin',
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
