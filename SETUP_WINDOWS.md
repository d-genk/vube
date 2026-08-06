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

## Updating to a newer version

When Daniel tells you there is an update, this is the whole procedure. **You do
not need to rebuild anything** — there is no compiled program any more, so
replacing the files is the entire update. The launcher notices on its own if the
libraries also need refreshing.

Close the cropping window first, then pick whichever of these two matches how
the folder got onto your computer.

### If you have Git

Press the Windows key, type `cmd`, press Enter, then:

```bash
cd C:\VubeCropper
git pull
```

Double-click `VubeCropper.cmd` as usual. If the update also changed the
libraries, the first launch afterwards shows the `[2/3] Installing...` line
again for a minute or two; that is expected and only happens on that one launch.

### If you downloaded a ZIP

1. Download the new ZIP from the link Daniel sends.
2. Unzip it somewhere temporary, like your Downloads folder.
3. Copy everything out of it into `C:\VubeCropper`, choosing **Replace the
   files in the destination** when Windows asks.
4. Double-click `VubeCropper.cmd`.

Do not delete the old folder first and do not delete the `.venv` folder inside
it — leaving `.venv` in place is what makes the update quick instead of another
ten-minute install.

### Checking that it worked

Nothing visible confirms an update on its own, so if you want to be sure, in
`cmd`:

```bash
cd C:\VubeCropper
git log -1 --format=%cd
```

That prints the date of the version you now have. Without Git, check the
modification date on `vube_cropper.py` in Explorer.

**Your cropped output and your settings are untouched by an update.** They live
outside the project folder, and an update only replaces program files.

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

This is what makes updates a plain `git pull`: a checkout that changes
`requirements.txt` updates its mtime, so the venv refreshes itself on the next
launch, and a checkout that does not touch it launches straight into the window.
Nothing needs rebuilding — the `.cmd` is a launcher, not a build artefact, and
only needs re-copying if the `.cmd` itself changed.

One caveat when telling the PI to update: `git pull` fails if he has somehow
modified a tracked file locally. If that happens, `git stash` then `git pull` is
the recovery, but it is worth asking what he changed first rather than
discarding it blind.

Passing arguments switches the launcher to console `python.exe` and forwards
the exit code, so it works in scripts as well as by double-click:

```bash
VubeCropper.cmd --cli E:\vube\archives -o C:\cropped
```

Remember that `--cli` has no review step — flagged pages are printed and keep
their crops. The window is the only path that offers "use the full page
instead" or "discard this page".

Two separate skip mechanisms exist and they are easy to confuse in a log:

- **resume** (`skip_done`, `--redo` to defeat) skips work whose *output* is
  already in the destination folder. Logged as "already done, skipping".
- **dedupe** (`dedupe`, `--no-dedupe` to defeat) skips a *source* file whose
  contents were already cropped earlier in the same run. Logged as "same file
  as ...".

Dedupe keys on (uncompressed size, CRC32). Size comes free from `stat` and from
the ZIP central directory; the CRC is only computed when two candidates share a
size, and for archive members it is read out of the directory rather than
calculated, so a drive of uniquely-sized files costs no extra reads at all.

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
