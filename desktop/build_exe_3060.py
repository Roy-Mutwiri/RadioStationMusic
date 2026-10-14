"""Build ``Trade Fix Radio RTX3060.exe`` with PyInstaller.

This is a separate build for RTX 3060 GPUs (12GB) that includes optimized settings
for running alongside other GPU-intensive apps (TikTok, NoxVM, Chrome, etc.).

    .venv\\Scripts\\python desktop\\build_exe_3060.py

Produces ``dist\\Trade Fix Radio RTX3060.exe`` and copies it to the project root.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "Trade Fix Radio RTX3060"


def main() -> int:
    icon = ROOT / "desktop" / "tradefix.ico"
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("frontend/dist is missing: run `npm install && npm run build` in frontend/ first")
        return 1

    # Include the 3060 config in the bundle
    config_3060 = ROOT / "configs" / "rtx3060.env"
    if not config_3060.is_file():
        print("configs/rtx3060.env is missing")
        return 1

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
        # The built Control Center, so an installed copy needs no Node.js
        "--add-data", f"{ROOT / 'frontend' / 'dist'};frontend/dist",
        # Include the RTX 3060 config
        "--add-data", f"{config_3060};configs",
        "--paths", str(ROOT / "desktop"),
        "--hidden-import", "bootstrap",
        "--hidden-import", "bootstrap_3060",
        str(ROOT / "desktop" / "app_3060.py"),
    ]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode
    built = ROOT / "dist" / f"{NAME}.exe"
    target = ROOT / f"{NAME}.exe"
    shutil.copy2(built, target)
    print(f"\n  built {built}\n  copied to {target}  ({target.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
