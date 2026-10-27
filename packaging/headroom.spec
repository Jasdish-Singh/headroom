# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a one-file headroom.exe.

Build on Windows from the repository root:

    py -3.12 -m pip install pyinstaller ".[tray,mcp]"
    py -3.12 -m PyInstaller --noconfirm packaging/headroom.spec

The exe is written to dist/headroom.exe.
"""

from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_submodules
except Exception:  # pragma: no cover - spec runs under PyInstaller
    collect_submodules = None

# SPECPATH is the directory that contains this spec (packaging/). The
# repository root is that directory's parent. Going up twice looked for src
# in the user's home folder.
root = Path(SPECPATH).resolve().parent
src = root / "src"
entry = src / "headroom" / "__main__.py"
package = src / "headroom"
datas = []
for path in sorted(package.rglob("*")):
    if not path.is_file() or path.suffix == ".py" or "__pycache__" in path.parts:
        continue
    relative = path.relative_to(package)
    dest = (Path("headroom") / relative.parent).as_posix()
    datas.append((str(path), dest))
hidden = [
    "headroom",
    "headroom.cli",
    "headroom.mcp_server",
    "headroom.mcp_tools",
    "psutil",
    "yaml",
    "tkinter",
]
if collect_submodules is not None:
    for package in ("headroom", "mcp"):
        try:
            hidden.extend(collect_submodules(package))
        except Exception:
            pass

a = Analysis(
    [str(entry)],
    pathex=[str(src)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="headroom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
