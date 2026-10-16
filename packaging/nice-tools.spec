# The end-user distribution is one executable. Keep all runtime assets inside it.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
analysis = Analysis(
    [str(root / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "nice_tools" / "static"), "nice_tools/static")] + copy_metadata("rapidfuzz") + copy_metadata("pycryptodome") + copy_metadata("charset-normalizer") + copy_metadata("pyodbc"),
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
        "nice_tools.ase_worker",
        "nice_tools.ase_batch",
        "nice_tools.ase_export",
        "nice_tools.ase_cli",
        "nice_tools.ase_odbc",
        "pyodbc",
        "nice_tools.iq_api",
        "nice_tools.iq_jobs",
        "nice_tools.iq_worker",
        "nice_tools.iq_odbc",
        "nice_tools.iq_files",
        "nice_tools.iq_sql",

        "nice_tools.ase_clients",
        "nice_tools.ase_process",
        "nice_tools.ase_picker",
        "nice_tools.ase_files",
    ] + collect_submodules("rapidfuzz") + collect_submodules("charset_normalizer"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "playwright"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="nice-tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
