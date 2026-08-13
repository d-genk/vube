"""
page_extract.py -- pull page rasters out of provider PDFs, crop off the mount,
and write them where the uploader can find them.

This is the single implementation. crop_images.py, pdf_test.py and
extract_and_crop.py all import from here; do not copy it again. Cropping
geometry lives in crop_core.py.

Two things worth knowing before changing anything here:

* Pages are never re-encoded unless they are actually cropped. A page that
  needs no crop is written byte-for-byte from the PDF, so it is bit-identical
  to what the provider shipped. A page that is cropped is re-saved in its
  original format, not converted to PNG -- these are photographs of paper, and
  PNG cannot compress that (it inflated the samples about 4.5x).

* Page order is numeric, not lexicographic. PDF object names sort as
  Im1, Im10, Im2 as strings, and output filenames are zero-padded to four
  digits so a directory listing stays in reading order past page 99.
"""

import contextlib
import hashlib
import io
import os
import re
import shutil
import tempfile
import zipfile
import zlib

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, JpegImagePlugin

from crop_core import detect_scan_box

# Formats the transcription API accepts. Anything else is converted to PNG,
# which is lossless, so an odd source format never costs us image data.
UPLOADABLE_EXTS = {"jpg", "jpeg", "png", "tif", "tiff"}
JPEG_QUALITY = 95      # only used when the source quantization is unavailable
JPEG_MCU = 16          # snap JPEG crops to this grid; see _snap_to_mcu
LOG_FILENAME = "crop_log.txt"   # written into the output folder each run


def natural_key(text):
    """Sort key that orders embedded numbers numerically: Im2 before Im10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", str(text))]


def _snap_to_mcu(box, width, height, grid=JPEG_MCU):
    """
    Enlarge a crop box outward to the nearest JPEG block boundary.

    JPEG stores the image in fixed blocks. If a crop starts on a block edge,
    re-encoding with the source's own quantization tables reproduces those
    blocks essentially unchanged, so the crop costs no visible quality and
    barely any file size. Snapping outward also errs toward keeping a few extra
    pixels, which is the safe direction here.
    """
    l, t, r, b = box
    l = (l // grid) * grid
    t = (t // grid) * grid
    r = min(width, -(-r // grid) * grid)
    b = min(height, -(-b // grid) * grid)
    return (l, t, r, b)


def _save_page(image_bytes, source_ext, box, out_stem):
    """
    Write one page image, re-encoding only if it is actually being cropped.

    Returns the path written, or None on failure.
    """
    ext = (source_ext or "png").lower()
    if ext == "jpg":
        ext = "jpeg"

    # Uncropped: copy the original bytes straight through. No decode, no
    # re-encode, no generation loss, and the smallest file we can possibly
    # write for this page.
    if box is None and ext in UPLOADABLE_EXTS:
        path = f"{out_stem}.{ext}"
        with open(path, "wb") as fh:
            fh.write(image_bytes)
        return path

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            source_format = img.format          # read before crop() drops it
            dpi = img.info.get("dpi")
            qtables = getattr(img, "quantization", None)
            subsampling = (JpegImagePlugin.get_sampling(img)
                           if source_format == "JPEG" else None)

            if box and source_format == "JPEG":
                box = _snap_to_mcu(box, img.width, img.height)
            out = img.crop(box) if box else img.copy()

            if ext in UPLOADABLE_EXTS and source_format:
                fmt, suffix = source_format, ext
            else:
                fmt, suffix = "PNG", "png"

            params = {}
            if fmt == "JPEG":
                if out.mode not in ("L", "RGB", "CMYK"):
                    out = out.convert("RGB")
                # Re-encode with the provider's own quantization rather than a
                # fixed quality, so the output matches the source instead of
                # inflating it. Falls back to a high fixed quality if the
                # tables are missing.
                if qtables:
                    params["qtables"] = qtables
                    if subsampling is not None:
                        params["subsampling"] = subsampling
                else:
                    params["quality"] = JPEG_QUALITY
            if dpi:
                params["dpi"] = dpi

            path = f"{out_stem}.{suffix}"
            try:
                out.save(path, format=fmt, **params)
            except (OSError, ValueError, KeyError):
                # Source format is readable but not writable by Pillow.
                path = f"{out_stem}.png"
                out.convert("RGB").save(path, format="PNG")
            return path
    except Exception as e:
        print(f"Error: could not save '{os.path.basename(out_stem)}': {e}")
        return None


def detect_and_remove_low_outliers(file_paths: list):
    """
    Deletes files that are statistically low-end outliers in size, on the
    assumption that a much-smaller-than-usual file is truncated or corrupt.

    Caution: file size mostly tracks ink density, so a legitimately sparse page
    (a title page, a blank verso) looks like a corrupt one. Only the blunt
    15%-of-median rule is enabled for that reason. Deleted pages are gone from
    the job silently, because the uploader globs whatever files exist.
    """
    if not file_paths:
        return

    valid_files = []
    sizes = []
    for path in file_paths:
        if os.path.exists(path):
            valid_files.append(path)
            sizes.append(os.path.getsize(path))

    if not valid_files:
        print("No created files found to analyze.")
        return

    if len(valid_files) < 3:
        # Statistical outlier detection needs more than two samples, but with
        # exactly two we can still compare them directly.
        if len(valid_files) == 2:
            s1, s2 = sizes[0], sizes[1]
            for i, (a, b) in enumerate(((s1, s2), (s2, s1))):
                if b > 0 and (a / b) < 0.15:
                    try:
                        os.remove(valid_files[i])
                        print(f"Deleted corrupted/outlier file: "
                              f"'{os.path.basename(valid_files[i])}' ({a / 1024:.2f} KB). "
                              f"Reason: Size is only {a / b:.1%} of the other file.")
                    except Exception as e:
                        print(f"Error deleting file '{valid_files[i]}': {e}")
                    break
        return

    sizes = np.array(sizes, dtype=np.float64)
    median_size = np.median(sizes)

    print("\n--- Statistical Outlier Analysis of Created Files ---")
    print(f"Total files analyzed: {len(valid_files)}")
    print(f"Mean size: {np.mean(sizes) / 1024:.2f} KB")
    print(f"Median size: {median_size / 1024:.2f} KB")

    deleted_count = 0
    for path, size in zip(valid_files, sizes):
        if size < 0.15 * median_size:
            try:
                os.remove(path)
                print(f"Deleted corrupted/outlier file: '{os.path.basename(path)}' "
                      f"({size / 1024:.2f} KB). Reason: less than 15% of median "
                      f"({median_size / 1024:.2f} KB)")
                deleted_count += 1
            except Exception as e:
                print(f"Error deleting outlier file '{path}': {e}")

    if deleted_count == 0:
        print("No corrupted/outlier files detected.")
    else:
        print(f"Successfully deleted {deleted_count} corrupted/outlier file(s).")


class PageResult:
    """One extracted page, and what we decided to do with it."""

    __slots__ = ("path", "pdf_path", "image_index", "box", "width", "height",
                 "size_bytes", "flags", "source_archive", "archive_member",
                 "discarded")

    def __init__(self, path, pdf_path, image_index, box, width, height, size_bytes,
                 source_archive=None, archive_member=None):
        self.path = path
        self.pdf_path = pdf_path
        # For pages that came out of a ZIP, pdf_path points into a scratch
        # directory that is deleted as soon as the archive is done. These two
        # let the review screen get the original back on demand.
        self.source_archive = source_archive
        self.archive_member = archive_member
        self.image_index = image_index
        self.box = box                 # None when no crop was applied
        self.width = width
        self.height = height
        self.size_bytes = size_bytes
        self.flags = []                # filled in by flag_pages()
        self.discarded = False         # set by discard_page()

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def keep_fraction(self):
        if not self.box:
            return 1.0
        l, t, r, b = self.box
        return ((r - l) * (b - t)) / float(self.width * self.height)

    @property
    def trimmed(self):
        """Pixels removed from each side, as (left, top, right, bottom)."""
        if not self.box:
            return (0, 0, 0, 0)
        l, t, r, b = self.box
        return (l, t, self.width - r, self.height - b)


# How much a page has to differ from the rest of its batch before it is worth a
# human glance. These are relative on purpose: how wide the provider's mount is
# varies from collection to collection, so an absolute "cropped more than 15%"
# rule flags either nothing or literally every page depending on the source.
# What actually indicates a problem is a page cropped much harder than its
# neighbours, since they all came off the same scanner in the same session.
KEEP_DROP = 0.15          # flag when a page keeps this much less than the median
KEEP_FLOOR = 0.50         # ...or when it keeps less than this, whatever the batch
SIZE_OUTLIER_RATIO = 0.15
NO_CROP_MINORITY = 0.70


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def batch_summary(results):
    """
    One-line description of what the run did, for the finished message.

    Worth showing even when nothing is flagged: if the typical crop is wildly
    wrong for a collection, that shows up here as a number the PI can sanity
    check at a glance, which per-page flags no longer do now that they are
    relative.
    """
    cropped = [r for r in results if r.box]
    if not cropped:
        return "No page needed cropping."
    typical = 1 - _median([r.keep_fraction for r in cropped])
    return (f"{len(cropped)} of {len(results)} page(s) cropped; "
            f"typically {typical:.0%} of each image removed.")


def flag_pages(results):
    """
    Mark pages that deserve a look, without touching any files.

    This replaces deleting size outliers. Nothing here is proof of a problem on
    its own -- a sparse page is legitimately small, and a page the detector
    declined to crop may simply have had no mount -- so these are review
    prompts, not verdicts.
    """
    if not results:
        return []

    median_size = _median([r.size_bytes for r in results])
    cropped = [r for r in results if r.box]
    mostly_cropped = bool(results) and (len(cropped) / len(results)) >= NO_CROP_MINORITY
    median_keep = _median([r.keep_fraction for r in cropped]) if cropped else 1.0

    for r in results:
        r.flags = []
        if r.box and (r.keep_fraction < median_keep - KEEP_DROP
                      or r.keep_fraction < KEEP_FLOOR):
            r.flags.append(
                f"crop removed {(1 - r.keep_fraction):.0%} of the image, against "
                f"{(1 - median_keep):.0%} for the rest of this batch")
        if median_size and r.size_bytes < SIZE_OUTLIER_RATIO * median_size:
            r.flags.append(f"file is much smaller than the others "
                           f"({r.size_bytes / 1024:.0f} KB vs "
                           f"{median_size / 1024:.0f} KB typical)")
        if not r.box and mostly_cropped:
            r.flags.append("no crop found, though most pages in this batch were cropped")

    return [r for r in results if r.flags]


# Discarded pages are moved here rather than unlinked. The uploader lists a
# single directory and does not recurse, so a page in this sub-folder is out of
# the job -- but it is still on disk if the call turns out to have been wrong.
DISCARD_DIRNAME = "_discarded"


def discard_page(result):
    """
    Take one page out of the output set.

    Moves the file into a _discarded sub-folder beside it instead of deleting
    it. That is enough to keep it out of the upload, and it means a mis-click
    during review costs nothing -- undiscard_page puts it straight back.
    Returns the new path, or None if the move failed.
    """
    if result.discarded:
        return result.path
    if not os.path.exists(result.path):
        return None

    holding = os.path.join(os.path.dirname(result.path), DISCARD_DIRNAME)
    os.makedirs(holding, exist_ok=True)
    target = os.path.join(holding, os.path.basename(result.path))
    try:
        if os.path.exists(target):
            os.remove(target)
        shutil.move(result.path, target)
    except Exception as e:
        print(f"Could not discard '{result.name}': {e}")
        return None

    result.path = target
    result.discarded = True
    return target


def undiscard_page(result):
    """Move a discarded page back into the output set."""
    if not result.discarded:
        return result.path
    if not os.path.exists(result.path):
        return None

    parent = os.path.dirname(os.path.dirname(result.path))
    target = os.path.join(parent, os.path.basename(result.path))
    try:
        if os.path.exists(target):
            os.remove(target)
        shutil.move(result.path, target)
    except Exception as e:
        print(f"Could not restore '{result.name}': {e}")
        return None

    result.path = target
    result.discarded = False
    return target


def append_log(output_dir, message):
    """
    Add a line to the run log.

    Review happens after the run has finished and closed its log handle, but
    the decisions made there -- especially discards -- are exactly what someone
    would need to see later, so they are appended the same way.
    """
    try:
        with open(os.path.join(output_dir, LOG_FILENAME), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass


@contextlib.contextmanager
def materialized_pdf(result):
    """
    Yield a readable path to the PDF a page came from, unpacking it again if it
    came out of an archive.

    Pages cropped out of a ZIP have a pdf_path pointing into scratch space that
    was deleted as soon as that archive finished, so the review screen cannot
    just reopen it. Re-extracting the single member is cheap and keeps us from
    having to hold every unpacked archive on disk until review is done.
    Yields None if the source can no longer be found.
    """
    if result.pdf_path and os.path.exists(result.pdf_path):
        yield result.pdf_path
        return

    if not (result.source_archive and result.archive_member
            and os.path.exists(result.source_archive)):
        yield None
        return

    tmp = tempfile.mkdtemp(prefix="vube_review_")
    try:
        target = os.path.join(tmp, os.path.basename(
            result.archive_member.replace("\\", "/")))
        with zipfile.ZipFile(result.source_archive, "r") as zf:
            with zf.open(result.archive_member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        yield target
    except Exception:
        yield None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore_full_page(result):
    """
    Re-write one page uncropped, straight from the original PDF.

    Used by the review screen when a crop is rejected. The original bytes are
    still in the PDF -- inside the archive if that is where it came from -- so
    nothing needs to have been kept unpacked on disk.
    """
    if result.discarded and not undiscard_page(result):
        return None
    with materialized_pdf(result) as pdf_path:
        if not pdf_path:
            print(f"Could not reach the original of '{result.name}' to restore it.")
            return None
        return _restore_from_pdf(result, pdf_path)


def _restore_from_pdf(result, pdf_path):
    doc = fitz.open(pdf_path)
    try:
        seen = set()
        counter = 1
        for page_index in range(len(doc)):
            images = sorted(doc[page_index].get_images(full=True),
                            key=lambda img: natural_key(img[7]))
            for info in images:
                blob = doc.extract_image(info[0])
                data = blob["image"]
                digest = hashlib.md5(data).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                if counter == result.image_index:
                    if os.path.exists(result.path):
                        os.remove(result.path)
                    stem = os.path.splitext(result.path)[0]
                    path = _save_page(data, blob.get("ext", "png"), None, stem)
                    if path:
                        result.path = path
                        result.box = None
                        result.size_bytes = os.path.getsize(path)
                        result.flags = []
                    return path
                counter += 1
    finally:
        doc.close()
    return None


def find_pdfs(root, recursive=True):
    """Every PDF under a file or directory, in reading order."""
    if os.path.isfile(root):
        return [root] if root.lower().endswith(".pdf") else []
    found = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=natural_key)
            for name in sorted(filenames, key=natural_key):
                if name.lower().endswith(".pdf"):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root), key=natural_key):
            path = os.path.join(root, name)
            if os.path.isfile(path) and name.lower().endswith(".pdf"):
                found.append(path)
    return found


def process_pdf(pdf_path, output_dir=None, on_page=None, should_stop=None,
                source_archive=None, archive_member=None):
    """
    Extract, crop and write every page raster of one PDF.

    output_dir   where to write (defaults to the PDF's own directory)
    on_page      callback(PageResult) after each page, for progress reporting
    should_stop  callable returning True to abort partway through

    Returns a list of PageResult. Identical rasters are de-duplicated by MD5.
    """
    out_dir = output_dir or os.path.dirname(os.path.abspath(pdf_path))
    os.makedirs(out_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"Error opening '{os.path.basename(pdf_path)}': {e}")
        return []

    results = []
    try:
        counter = 1
        seen = set()
        for page_index in range(len(doc)):
            if should_stop and should_stop():
                break
            # Sort by the PDF object name, numerically. A plain string sort
            # gives Im1, Im10, Im2 and scrambles pages 10+ within a page.
            images = sorted(doc[page_index].get_images(full=True),
                            key=lambda img: natural_key(img[7]))

            for info in images:
                if should_stop and should_stop():
                    break
                try:
                    blob = doc.extract_image(info[0])
                except Exception as e:
                    print(f"Warning: could not extract image {counter} from "
                          f"'{base_name}': {e}")
                    continue

                data = blob["image"]
                digest = hashlib.md5(data).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)

                cv_img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if cv_img is None:
                    # Undecodable by OpenCV. Keep the page uncropped rather
                    # than dropping it entirely.
                    print(f"Warning: could not decode image {counter} in "
                          f"'{base_name}'; writing it uncropped.")
                    box, h, w = None, 0, 0
                else:
                    h, w = cv_img.shape[:2]
                    box = detect_scan_box(cv_img)

                stem = os.path.join(out_dir, f"{base_name}_{counter:04d}")
                path = _save_page(data, blob.get("ext", "png"), box, stem)

                if path:
                    if not w or not h:
                        with Image.open(path) as probe:
                            w, h = probe.size
                    res = PageResult(path, pdf_path, counter, box, w, h,
                                     os.path.getsize(path),
                                     source_archive=source_archive,
                                     archive_member=archive_member)
                    results.append(res)
                    if on_page:
                        on_page(res)
                counter += 1
    finally:
        doc.close()
    return results


def process_pdf_images_dynamic(directory_path: str, run_outlier_check: bool = False,
                               output_dir=None, recursive=False,
                               on_page=None, on_pdf=None, should_stop=None):
    """
    Crop every page of every PDF under a directory.

    Kept at this name and signature because automate_pipeline.py calls it.
    run_outlier_check defaults to off: deleting pages on a file-size heuristic
    loses sparse pages, and the uploader globs whatever survives, so a deletion
    leaves no trace. Prefer flag_pages() plus a human glance.
    """
    if not os.path.isdir(directory_path):
        raise NotADirectoryError(f"The directory path '{directory_path}' does not exist.")

    pdfs = find_pdfs(directory_path, recursive=recursive)
    all_results = []
    for pdf_path in pdfs:
        if should_stop and should_stop():
            break
        if on_pdf:
            on_pdf(pdf_path)
        results = process_pdf(pdf_path, output_dir=output_dir,
                              on_page=on_page, should_stop=should_stop)
        short = os.path.basename(pdf_path)
        for r in results:
            if r.box:
                l, t, rr, b = r.trimmed
                print(f"Success: '{short}' image {r.image_index} -> '{r.name}' "
                      f"(trimmed {l}/{t}/{rr}/{b} L/T/R/B)")
            else:
                print(f"Success: '{short}' image {r.image_index} -> '{r.name}' "
                      f"(no crop needed)")
        if not results:
            print(f"Skipped: '{short}' (No embedded images found)")
        all_results.extend(results)

    if all_results:
        cropped = sum(1 for r in all_results if r.box)
        print(f"\nWrote {len(all_results)} page image(s); {cropped} cropped, "
              f"{len(all_results) - cropped} passed through unmodified.")

    if run_outlier_check and all_results:
        detect_and_remove_low_outliers([r.path for r in all_results])

    return all_results


# ---------------------------------------------------------------------------
# ZIP archives
#
# The production dataset is mostly ZIPs, one archive per volume, each holding an
# arbitrary number of PDFs. Archives are unpacked to a temporary directory one
# at a time and deleted immediately after, so a drive of archives never needs
# room for more than one unpacked copy.
# ---------------------------------------------------------------------------

ARCHIVE_EXTS = {".zip"}


def find_archives(root, recursive=True):
    """Every ZIP under a file or directory, in reading order."""
    if os.path.isfile(root):
        return [root] if os.path.splitext(root)[1].lower() in ARCHIVE_EXTS else []
    found = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=natural_key)
            for name in sorted(filenames, key=natural_key):
                if os.path.splitext(name)[1].lower() in ARCHIVE_EXTS:
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root), key=natural_key):
            path = os.path.join(root, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in ARCHIVE_EXTS:
                found.append(path)
    return found


def _is_safe_member(name):
    """
    Reject archive entries that would write outside the extraction directory.

    A ZIP can name its members anything, including absolute paths and ..
    segments. Python's extractall does guard against this now, but we extract
    members individually so the check has to be here.
    """
    if not name or name.startswith("__MACOSX"):
        return False
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or ":" in normalised.split("/")[0]:
        return False
    return ".." not in normalised.split("/")


def extract_pdfs_from_archive(zip_path, dest_dir, accept=None):
    """
    Unpack only the PDF members of one archive into dest_dir.

    Only PDFs are extracted -- the archives also carry metadata and thumbnails
    we have no use for, and skipping them saves both time and scratch space.
    Names are flattened to their basename, de-duplicated if the archive nests
    the same filename in two folders.

    `accept(member_name, size, crc)` may be supplied to skip members before
    they are written -- used to leave out duplicates of PDFs already cropped.

    Returns a list of (extracted path, original member name).
    """
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [m for m in zf.infolist()
                       if not m.is_dir()
                       and m.filename.lower().endswith(".pdf")
                       and _is_safe_member(m.filename)]
            members.sort(key=lambda m: natural_key(m.filename))

            used = set()
            for m in members:
                if accept and not accept(m.filename, m.file_size, m.CRC):
                    continue
                base = os.path.basename(m.filename.replace("\\", "/"))
                stem, ext = os.path.splitext(base)
                candidate, n = base, 2
                while candidate.lower() in used:
                    candidate = f"{stem}__{n}{ext}"
                    n += 1
                used.add(candidate.lower())

                target = os.path.join(dest_dir, candidate)
                with zf.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append((target, m.filename))
    except zipfile.BadZipFile:
        print(f"Error: '{os.path.basename(zip_path)}' is not a readable ZIP file.")
        return []
    except Exception as e:
        print(f"Error reading '{os.path.basename(zip_path)}': {e}")
        return []
    return written


def count_pdfs_in_archive(zip_path):
    """How many PDFs an archive holds, without unpacking it."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return sum(1 for m in zf.infolist()
                       if not m.is_dir()
                       and m.filename.lower().endswith(".pdf")
                       and _is_safe_member(m.filename))
    except Exception:
        return 0


def process_archive(zip_path, output_dir, on_page=None, on_pdf=None,
                    should_stop=None, scratch_root=None, accept_member=None):
    """
    Unpack one archive to a temporary directory, crop every PDF inside it, and
    delete the temporary copy.

    Output goes to output_dir/<archive name>/ so two archives cannot collide.
    Returns a list of PageResult.
    """
    name = os.path.splitext(os.path.basename(zip_path))[0]
    out_dir = os.path.join(output_dir, name)
    results = []

    tmp = tempfile.mkdtemp(prefix="vube_", dir=scratch_root)
    try:
        pdfs = extract_pdfs_from_archive(zip_path, tmp, accept=accept_member)
        if not pdfs:
            return []
        for pdf, member in pdfs:
            if should_stop and should_stop():
                break
            if on_pdf:
                on_pdf(pdf)
            results.extend(process_pdf(pdf, output_dir=out_dir,
                                       on_page=on_page, should_stop=should_stop,
                                       source_archive=zip_path,
                                       archive_member=member))
    finally:
        # Always clean up, including on error or a mid-run stop, so a long job
        # over many archives cannot fill the disk with unpacked copies.
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def find_work(root, recursive=True):
    """
    Everything crop-able under a path: loose PDFs and ZIP archives.

    Returns (loose_pdfs, archives). A PDF that lives inside an archive is not
    listed in loose_pdfs; it is reached by unpacking the archive.
    """
    return find_pdfs(root, recursive=recursive), find_archives(root, recursive=recursive)


# ---------------------------------------------------------------------------
# Duplicate detection
#
# The same volume often sits on the drive twice -- loose and inside an archive,
# or in two archives. Cropping it twice wastes time and puts two copies of every
# page into the output, which then get uploaded twice.
#
# Matching is on content, not filename, so the same PDF under two different
# names is still caught. Size is the first-pass key because it is free: for a
# ZIP it comes out of the central directory without unpacking anything, and for
# a loose file it is one stat call. Only when two candidates share a size do we
# read bytes to compute a CRC. On a drive where almost every file is unique that
# means almost no extra reading.
# ---------------------------------------------------------------------------


def crc32_of_file(path, chunk=1 << 20):
    """CRC32 of a file on disk, read in chunks so a big PDF does not sit in RAM."""
    value = 0
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                value = zlib.crc32(block, value)
    except OSError:
        return None
    return value & 0xFFFFFFFF


def archive_pdf_entries(zip_path):
    """
    (member name, uncompressed size, CRC32) for each PDF in an archive.

    All three come from the ZIP central directory, so this does not unpack or
    even decompress anything.
    """
    entries = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for m in zf.infolist():
                if (not m.is_dir() and m.filename.lower().endswith(".pdf")
                        and _is_safe_member(m.filename)):
                    entries.append((m.filename, m.file_size, m.CRC))
    except Exception:
        return []
    return sorted(entries, key=lambda e: natural_key(e[0]))


class DuplicateTracker:
    """
    Remembers which PDFs have been cropped in this run.

    Sizes seen only once never need a CRC, so `prime` is given every candidate
    size up front and `is_duplicate` only pays for the ambiguous ones.
    """

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._ambiguous_sizes = set()
        self._seen = {}          # (size, crc or None) -> description of the first
        self.duplicates = []     # (description, description of the original)

    def prime(self, sizes):
        """Record which sizes appear more than once across everything found."""
        counts = {}
        for size in sizes:
            counts[size] = counts.get(size, 0) + 1
        self._ambiguous_sizes = {s for s, n in counts.items() if n > 1}

    def _fingerprint(self, size, crc_getter):
        if size not in self._ambiguous_sizes:
            return (size, None)      # unique size: no need to read the bytes
        return (size, crc_getter())

    def check(self, description, size, crc_getter):
        """
        Returns the description of the earlier copy if this is a duplicate,
        otherwise None and records it as the original.
        """
        if not self.enabled:
            return None
        key = self._fingerprint(size, crc_getter)
        first = self._seen.get(key)
        if first is not None:
            self.duplicates.append((description, first))
            return first
        self._seen[key] = description
        return None
