"""Windows x64 onefile build; no user config, ESP-IDF or native Tunnel client."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = copy_metadata("cardputer-device-mcp", recursive=True)
analysis = Analysis(
    [str(root / "packaging" / "windows_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        *collect_submodules("cardputer_mcp"),
        *collect_submodules("mcp"),
        *collect_submodules("uvicorn"),
        "tkinter",
        "tkinter.ttk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="cardputer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    uac_admin=False,
)
