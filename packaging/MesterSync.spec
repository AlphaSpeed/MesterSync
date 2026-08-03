from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all


ROOT = Path(os.environ.get("MESTERSYNC_PROJECT_ROOT", Path.cwd())).resolve()
APP = ROOT / "app"
datas, binaries, hiddenimports = collect_all("tkinterdnd2")
datas += [
    (str(APP / "mestersync_logo.png"), "."),
    (str(APP / "mestersync_icon.png"), "."),
    (str(APP / "mestersync_icon.ico"), "."),
    (str(APP / "README_MesterSync.txt"), "."),
    (str(ROOT / "presets"), "presets"),
]

analysis = Analysis(
    [str(APP / "MesterSync.pyw")],
    pathex=[str(APP)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MesterSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP / "mestersync_icon.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
)
collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MesterSync",
)

