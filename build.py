"""
Build DiscJockey.exe with PyInstaller.

Builds in %LOCALAPPDATA%\\DiscJockeyBuild, outside OneDrive, so sync and
antivirus locks don't break the build.

Usage (from the venv, in the project folder):
    python build.py            # windowed build, no console
    python build.py --console  # keeps the console, so debug prints show
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_SCRIPT = "app.py"
APP_NAME = "DiscJockey"
ICON = None                                        # e.g. "discjockey.ico", or leave as None
VLC_DIR = Path(r"C:\Program Files\VideoLAN\VLC")   # 64-bit VLC install folder
VLC_FILES = ("libvlc.dll", "libvlccore.dll")       # plus the plugins folder


def main():
    console = "--console" in sys.argv
    here = Path(__file__).resolve().parent
    out = Path(os.environ["LOCALAPPDATA"]) / "DiscJockeyBuild"
    stage, work, dist = out / "vlc_stage", out / "work", out / "dist"

    for dll in VLC_FILES:
        if not (VLC_DIR / dll).exists():
            sys.exit(f"Missing {VLC_DIR / dll}. Check VLC_DIR at the top of build.py.")
    if not (VLC_DIR / "plugins").is_dir():
        sys.exit(f"Missing {VLC_DIR / 'plugins'}. Check VLC_DIR at the top of build.py.")

    # Start from a clean output folder (retries if something briefly holds a file)
    def onerror(func, path, exc):
        sys.exit(f"Couldn't delete {path}. Quit DiscJockey from the tray, close any "
                 f"Explorer windows open in {out}, and try again.")
    if out.exists():
        shutil.rmtree(out, onexc=onerror)
    stage.mkdir(parents=True)

    # Stage only the VLC runtime files the app needs, bundled as vlc/ inside the exe
    for dll in VLC_FILES:
        shutil.copy2(VLC_DIR / dll, stage / dll)
    shutil.copytree(VLC_DIR / "plugins", stage / "plugins")

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--name", APP_NAME,
           "--workpath", str(work),
           "--distpath", str(dist),
           "--specpath", str(out),
           "--add-data", f"{stage};vlc"]
    if not console:
        cmd.append("--windowed")
    if ICON:
        cmd += ["--icon", str(here / ICON)]
    cmd.append(str(here / APP_SCRIPT))

    subprocess.run(cmd, cwd=here, check=True)
    shutil.rmtree(stage, ignore_errors=True)

    exe = dist / APP_NAME / (APP_NAME + ".exe")
    print(f"\nBuild finished: {exe}")
    print(f"Copy the whole folder {dist / APP_NAME} wherever you want to keep it.")


if __name__ == "__main__":
    main()