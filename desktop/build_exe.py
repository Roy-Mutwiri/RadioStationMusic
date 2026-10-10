"""Build ``Trade Fix Radio.exe`` with PyInstaller.

    .venv\\Scripts\\python desktop\build_exe.py

Produces ``dist\\Trade Fix Radio.exe`` and copies it to the project root, which is where it
must live: the exe is the desktop launcher only, and it starts the station from the ``.venv``
and ``tradefix_radio`` package beside it. The station itself is not frozen — it is a Python
3.10 project with native audio dependencies, and ACE-Step runs in its own environment.

The exe carries the app icon (``desktop/tradefix.ico``) so the window, taskbar and Explorer
all show it, and it is a windowed (no console) build. It is not ``pythonw.exe``, so it is
not caught by tooling that stops every ``pythonw`` process.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "Trade Fix Radio"


def main() -> int:
    icon = ROOT / "desktop" / "tradefix.ico"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        "--icon", str(icon),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        # pywebview picks its backend at runtime; make sure the Windows one is bundled.
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium",
        "--collect-all", "webview",
        "--collect-all", "clr_loader",
        "--collect-all", "pythonnet",
        # The icon is read at runtime too (title bar + taskbar via WM_SETICON).
        "--add-data", f"{icon};desktop",
        str(ROOT / "desktop" / "app.py"),
    ]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, cwd=ROOT, check=False)  # noqa: S603 - fixed argv
    if result.returncode != 0:
        return result.returncode
    built = ROOT / "dist" / f"{NAME}.exe"
    target = ROOT / f"{NAME}.exe"
    shutil.copy2(built, target)
    print(f"\n  built {built}\n  copied to {target}  ({target.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
