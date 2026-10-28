"""Linux x64 onefile build; no user config, ESP-IDF or native Tunnel client."""

from pathlib import Path
import runpy
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = copy_metadata("cardputer-device-mcp", recursive=True)
datas.append((str(root / "src" / "cardputer_mcp" / "flash_worker.py"), "cardputer_mcp"))
analysis = Analysis(
    [str(root / "packaging" / "windows_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        *collect_submodules("cardputer_mcp"),
        # The SDK's optional standalone CLI imports typer and exits if absent.
        # Our application supplies its own CLI and never uses that extra.
        *collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli")),
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
)
sys.path.insert(0, str(root / "packaging"))
runpy.run_path(str(root / "packaging" / "release_linux.py"))["record_build_runtime"](
    Path(exe.name), root
)
