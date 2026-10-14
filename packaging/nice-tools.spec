# The end-user distribution is one executable. Keep all runtime assets inside it.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
native = root / "build" / "ase-native"
if not (native / "sybdb.dll").is_file() or not (native / "native-manifest.json").is_file():
    raise RuntimeError("Build the verified ASE native libraries with scripts/build_ase_native.ps1 first.")
native_binaries = [(str(path), "nice_tools/native") for path in native.glob("*.dll")]
native_data = [(str(path), "nice_tools/native/" + str(path.parent.relative_to(native)).replace("\\", "/"))
               for path in native.rglob("*") if path.is_file() and path.suffix.lower() != ".dll"]

analysis = Analysis(
    [str(root / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=native_binaries,
    datas=[(str(root / "nice_tools" / "static"), "nice_tools/static")] + native_data + copy_metadata("rapidfuzz") + copy_metadata("pycryptodome") + copy_metadata("charset-normalizer"),
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
        "nice_tools.ase_worker",
        "nice_tools.ase_native",
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
