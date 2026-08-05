# Setting up the cropping tool

One-time setup, about ten minutes, most of it waiting. You only do this once.

If anything below does not match what you see on screen, stop and send Daniel a
screenshot rather than guessing.

## Step 1 — Install Python

Check whether it is already there first. Press the Windows key, type `cmd`,
press Enter, and in the black window type:

```bash
py --version
```

If that prints something like `Python 3.11.4`, skip to Step 2.

If it says Python is not recognised, install it:

1. Go to <https://www.python.org/downloads/windows/>
2. Download the **Windows installer (64-bit)** for the latest 3.12 or 3.13.
3. Run it. On the first screen, **tick "Add python.exe to PATH"** before
   clicking Install. This one checkbox causes most setup problems when missed.
4. Leave every other option at its default — in particular leave
   **"tcl/tk and IDLE"** ticked, because the tool's window needs it.

This installer is digitally signed by the Python Software Foundation, so it is
not affected by the block on unsigned programs.

## Step 2 — Get the tool onto the computer

Put the project folder somewhere permanent that is **not** the external drive
and **not** inside OneDrive — for example `C:\VubeCropper`. Everything in this
repository goes in that one folder.

## Step 3 — First run

Open that folder and double-click **`VubeCropper.cmd`**.

A black console window appears and shows:

```
  First-time setup. This takes a few minutes and only happens once.

  [1/3] Creating a private Python environment...
  [2/3] Installing the imaging libraries...
  [3/3] Setup finished. Starting...
```

Step 2 downloads about 80 MB, so give it a few minutes on a slow connection.
When it finishes, the cropping window opens and the console closes itself.

**Every run after this one skips straight to the window** — the setup only
happens the first time.

That is it. From here on, use [README_CROPPING.md](README_CROPPING.md), which
describes the window itself. Wherever that document says
"double-click VubeCropper.exe", double-click `VubeCropper.cmd` instead.

## Making it easier to launch

Right-click `VubeCropper.cmd` → **Show more options** → **Send to** → **Desktop
(create shortcut)**. Rename the new desktop icon to whatever you like. You can
also drag that shortcut onto the taskbar to pin it.

## If something goes wrong

**"Python was not found on this computer."**
Step 1 did not take, almost always because "Add python.exe to PATH" was not
ticked. Re-run the Python installer, choose **Modify**, and make sure it is on.

**"Could not install the libraries."**
Nearly always the network or an institutional proxy blocking the download. Send
Daniel the text in the console window.

**The console flashes and disappears, no window opens.**
Open `cmd`, then run the launcher by its full path so the error stays on
screen:

```bash
C:\VubeCropper\VubeCropper.cmd
```

**Setup got interrupted half-way.**
Delete the `.venv` folder inside the project folder and double-click
`VubeCropper.cmd` again. Nothing else is affected — `.venv` is only the
downloaded libraries, never your scans or your cropped output.

---

## Notes for whoever maintains this

The `.venv` is private to the project folder and is in `.gitignore`; it is
never committed and can be deleted at any time to force a clean reinstall.

`VubeCropper.cmd` re-runs `pip install` only when `requirements.txt`'s
modification time differs from the stamp in `.venv\.requirements-installed`.
Editing `requirements.txt` therefore reinstalls on the next launch; touching
other files does not.

Passing arguments switches the launcher to console `python.exe` and forwards
the exit code, so it works in scripts as well as by double-click:

```bash
VubeCropper.cmd --cli E:\vube\archives -o C:\cropped
```

Remember that `--cli` has no review step — flagged pages are printed and keep
their crops. The window is the only path that offers "use the full page
instead".

### Dependencies

`requirements.txt` covers the whole runtime: `pillow`,
`opencv-python-headless`, `PyMuPDF`, `numpy`. Everything else the app imports
is standard library, except `tkinter`, which ships with Python and cannot be
pip-installed.

Two traps worth remembering:

- PyMuPDF imports as `fitz`, but there is an unrelated package on PyPI actually
  called `fitz`. Installing that one does not work.
- OpenCV is pinned to the `-headless` build deliberately. Nothing calls
  `cv2.imshow`, so the GUI build only adds Qt dependencies that can conflict
  with tkinter.

The pins were checked against both the old stack (numpy 1.26 / PyMuPDF 1.25 /
OpenCV 4.10) and current releases (numpy 2.4 / PyMuPDF 1.28 / OpenCV 4.14);
`detect_scan_box` returns identical geometry on both.
