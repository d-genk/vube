# Cropping page scans — instructions

The scans from the provider come mounted on a white board with a copyright line
stamped along the bottom. That board is not part of the page, and leaving it on
makes the transcriptions worse. This tool trims it off.

## The short version

First time only, follow [SETUP_WINDOWS.md](SETUP_WINDOWS.md) — about ten
minutes, once. After that:

1. Plug in the external drive.
2. Double-click **VubeCropper.cmd**.
3. **Browse...** to the folder on the drive. It can hold ZIP archives, loose
   PDFs, or both, nested as deeply as you like.
4. **Browse...** to where you want the cropped pages saved. Put this somewhere
   on the computer, not on the external drive.
5. Press **Start**.

That is the whole job. The external drive is only ever read from — the tool
cannot write to it or change anything on it.

## What you will see

A progress bar and a running list of the files as they are processed. A typical
page takes well under a second, so a few hundred pages is a couple of minutes.

**Stop** halts the run. Pages already written stay where they are; nothing is
half-finished or corrupted.

## Reviewing unusual pages

When the run finishes, the tool may say something like *"12 of them look
unusual"*. This is not an error. It means those pages tripped one of three
checks:

- the crop removed an unusually large share of the image
- the saved file is much smaller than the others in the batch
- no crop was found, although most pages in the batch were cropped

**Reviewing is optional, and the crops are already finished.** Every cropped
page is written to disk before the review window ever opens, in exactly the
state it would be uploaded in. The window is there so you can *override* a
handful of decisions — it is not a queue of work that has to be cleared.

If you are happy with the crops, press **No** at the prompt, or **Keep all
remaining crops and close** inside the window. Nothing further happens and every
crop stands. On a large job this is the normal answer.

**Nothing is ever deleted.** The tool shows you each flagged page and asks.

The review window shows two pictures side by side. On the left is the whole
scan with the proposed crop outlined in green; on the right is what would
actually be sent. Both are brightness-boosted on purpose — the provider's mount
is white and the paper is nearly white, so at normal brightness you cannot see
where one ends and the other begins.

- **Keep the crop** — the crop was right, move on. This is the usual answer.
- **Use the full page instead** — the crop looks wrong. The page is rewritten
  uncropped, straight from the original PDF.
- **Discard this page** — the page itself is no good: blank, a duplicate, a
  scanning mishap. It is taken out of the set that gets uploaded.
- **Keep all remaining crops and close** — you have seen enough. Every page you
  have not looked at keeps its crop, which is what would have happened anyway;
  this just says so in the log and closes the window.

Discarding does not delete anything. The page is moved into a `_discarded`
folder alongside the others, which is enough to keep it out of the upload, and
the button turns into **Put this page back** if you change your mind. Choosing
*Keep the crop* or *Use the full page instead* on a discarded page also brings
it back.

You can close the review window at any point, with the X if you like. Anything
you did not look at keeps its crop. Every decision you make is written to the
log file, so there is always a record of what was discarded and why.

The log also lists every flagged page and why it was flagged. On a big job that
is far more practical than clicking through them: open `crop_log.txt`, read the
reasons, and go look at a few in the output folder if any of them sound wrong.

## Sending pages for transcription

The checkbox **"Also send the cropped pages to the transcription API"** is off
by default and is not remembered between runs. Cropping happens on this
computer and costs nothing; uploading does, so it has to be asked for each
time. Leave it off unless you specifically mean to upload.

## The same volume appearing twice

Drives usually have some overlap — a volume sitting loose *and* inside an
archive, or the same volume in two archives. By default each file is cropped
**once**: the first copy found is processed and later copies are skipped, with a
line in the log saying which earlier file it matched.

Matching is on the contents of the file, not its name, so a renamed copy is
still recognised as the same volume. Two genuinely different volumes are never
merged, even if their files happen to be the same size.

Loose PDFs are looked at before archives, so when a volume exists both ways it
is the loose copy that gets cropped.

If you actually want every copy cropped separately, untick **Crop each file only
once**.

## ZIP archives

Most of the dataset is ZIPs, and the app handles them directly — there is no
need to unzip anything first. Each archive is unpacked to a temporary folder,
cropped, and the temporary copy deleted straight away, so a drive of hundreds of
archives never needs room for more than one unpacked at a time.

Damaged archives and archives with no PDFs inside are reported and skipped; the
run carries on rather than stopping.

## Where the files go

The output folder mirrors the folder structure of the source, and each archive
gets its own sub-folder named after it, so two archives cannot overwrite each
other. Pages are named with the source PDF and a four-digit page number:

    PDF/00010101_99991231/vol_1812_0/doc_1_0001.jpeg
    PDF/00010101_99991231/vol_1812_0/doc_1_0002.jpeg

They are saved in the same format the provider used, at the same quality. A
page that needed no crop is copied across untouched, byte for byte.

Two things in the output folder are not pages: `crop_log.txt`, and a
`_discarded` folder if you discarded anything during review. Neither is ever
uploaded.

## If something goes wrong

**"No PDF files or ZIP archives found in that folder."** Either the folder is
wrong, or the files are in sub-folders — tick **Include sub-folders** and try
again.

**A run was interrupted.** Just start it again. Archives that already finished
are skipped, so it picks up roughly where it stopped. Untick **Skip archives I
have already cropped** if you want everything redone from scratch.

**Fewer pages than you expected, and the log says "already done, skipping".**
That is the resume feature: those files were cropped into this same output
folder on an earlier run. It is not the same thing as the duplicate check, which
says "same file as ...". To crop everything again, either choose an empty output
folder or untick **Skip archives I have already cropped**.

**Windows warns about an unrecognised app.** This should no longer happen —
the tool now runs as plain Python source under the signed Python interpreter,
with no compiled program of its own. If you do see the warning, stop and tell
Daniel rather than clicking through it.

**Anything else** — every run appends to `crop_log.txt` in the output folder.
Send Daniel that file; it has the full detail, including anything that scrolled
out of the window and every review decision you made.

Windows hides file extensions by default, so in Explorer this file shows up as
just **crop_log** with a Notepad icon. That is the right file — double-click it
to read it.

---

## For whoever maintains this

The PI runs the same source that runs in the pipeline — there is no build step
any more. `VubeCropper.cmd` provisions a `.venv` from `requirements.txt` on
first launch and opens the window on every launch after that; see
[SETUP_WINDOWS.md](SETUP_WINDOWS.md).

`build_exe.py` still produces `dist/VubeCropper.exe` via PyInstaller, but that
executable is unsigned and is blocked by IT policy, so it is not the delivery
route. Keep it only if a code-signing certificate turns up.

Layout:

| file | what it does |
|---|---|
| `crop_core.py` | finds the page inside the mount; the actual algorithm |
| `page_extract.py` | pulls rasters out of PDFs, crops, saves, flags |
| `vube_cropper.py` | the desktop app, and a `--cli` mode for scripting |
| `test_crop.py` | regression harness — run after touching `crop_core.py`; point `VUBE_SAMPLES` at a folder of provider PDFs |
| `preview_crop.py` | writes before/after images for a whole folder |
| `automate_pipeline.py` | the full archive → crop → submit pipeline |
| `requirements.txt` | the four runtime dependencies; read the notes in it before changing pins |
| `VubeCropper.cmd` | double-click launcher; builds the `.venv` on first run |

Headless use, same code path as the window:

```bash
python vube_cropper.py --cli E:/vube/archives -o C:/cropped
```

Two settings worth knowing about in `crop_core.py`. `PAD_FRAC` is the safety
margin left around every crop — raise it if a shaved edge ever turns up; it is
the cheapest and safest knob in the file. `PAPER_TOO_BRIGHT` is the point at
which the detector gives up and leaves a page uncropped, because paper that
bright cannot be told apart from a white mount.

Outlier *deletion* still exists in `page_extract.detect_and_remove_low_outliers`
but is off everywhere by default. It keys off file size, which mostly tracks ink
density, so it cannot distinguish a sparse page from a truncated one — and the
uploader globs whatever files exist, so a deletion vanishes without a trace.
`flag_pages()` plus the review screen replaces it.
