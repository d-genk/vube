"""
build_exe.py -- package vube_cropper.py into a single Windows executable.

    python build_exe.py

Produces dist/VubeCropper.exe, which runs on a machine with no Python
installed. Hand that one file to the PI; nothing else needs to be copied.

The build takes a few minutes and needs PyInstaller:

    pip install pyinstaller
"""

import os
import shutil
import subprocess
import sys

APP = "VubeCropper"
ENTRY = "vube_cropper.py"

# Everything the app imports at runtime that PyInstaller cannot see by walking
# the import graph, plus the project modules the entry point pulls in.
HIDDEN = [
    "PIL._tkinter_finder",   # ImageTk needs this and it is imported lazily
    "cv2",
    "fitz",
]

# Trimming these keeps the executable to a sane size. None of them are used by
# the cropper; they arrive as transitive dependencies.
EXCLUDE = [
    "matplotlib", "scipy", "pandas", "pytest", "IPython", "notebook",
    "PyQt5", "PySide2", "tornado", "sqlalchemy",
]


def main():
    if not os.path.exists(ENTRY):
        print(f"[!] run this from the project directory ({ENTRY} not found)")
        return 1

    for stale in ("build", "dist", f"{APP}.spec"):
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
        elif os.path.exists(stale):
            os.remove(stale)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",           # one self-contained .exe
        "--windowed",          # no console window behind the GUI
        "--name", APP,
        ENTRY,
    ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]

    print("Running:", " ".join(cmd), "\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("\n[!] PyInstaller failed. Is it installed?  pip install pyinstaller")
        return result.returncode

    exe = os.path.join("dist", f"{APP}.exe")
    if os.path.exists(exe):
        size = os.path.getsize(exe) / (1024 * 1024)
        print(f"\nBuilt {exe}  ({size:.0f} MB)")
        print("Hand that single file to the PI. Nothing else is needed.")
        return 0
    print("\n[!] build reported success but the .exe is missing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
