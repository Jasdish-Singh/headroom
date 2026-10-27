# Build and test

```powershell
py -3.12 -m pip install -e ".[dev]"
py -3.12 -m pytest
```

Tests use a temporary home. They do not read the machine's RAM and they do not delete its temp folder.

The Windows exe:

```powershell
py -3.12 -m pip install pyinstaller ".[tray,mcp]"
py -3.12 -m PyInstaller --noconfirm packaging\headroom.spec
```

That writes `dist\headroom.exe`. The installer script is `packaging\headroom.iss` (`iscc packaging\headroom.iss` on a machine with Inno Setup). CI runs the same tests on `ubuntu-latest` and `windows-latest`, and builds the exe on `windows-latest`. The release workflow is manual and can sign the exe. See [PUBLISHING.md](PUBLISHING.md).
