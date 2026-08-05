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

import hashlib
import io
import os
import re

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
                 "size_bytes", "flags")

    def __init__(self, path, pdf_path, image_index, box, width, height, size_bytes):
        self.path = path
        self.pdf_path = pdf_path
        self.image_index = image_index
        self.box = box                 # None when no crop was applied
        self.width = width
        self.height = height
        self.size_bytes = size_bytes
        self.flags = []                # filled in by flag_pages()

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


# A crop this aggressive is legitimate for a wide mount but is also what a
# mis-crop looks like, so it earns a human glance rather than a deletion.
AGGRESSIVE_KEEP = 0.85
SIZE_OUTLIER_RATIO = 0.15
NO_CROP_MINORITY = 0.70


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

    sizes = sorted(r.size_bytes for r in results)
    median = sizes[len(sizes) // 2] if sizes else 0
    cropped = sum(1 for r in results if r.box)
    mostly_cropped = bool(results) and (cropped / len(results)) >= NO_CROP_MINORITY

    for r in results:
        r.flags = []
        if r.box and r.keep_fraction < AGGRESSIVE_KEEP:
            r.flags.append(f"crop removed {(1 - r.keep_fraction):.0%} of the image")
        if median and r.size_bytes < SIZE_OUTLIER_RATIO * median:
            r.flags.append(f"file is much smaller than the others "
                           f"({r.size_bytes / 1024:.0f} KB vs {median / 1024:.0f} KB typical)")
        if not r.box and mostly_cropped:
            r.flags.append("no crop found, though most pages in this batch were cropped")

    return [r for r in results if r.flags]


def restore_full_page(result):
    """
    Re-write one page uncropped, straight from the PDF.

    Used by the review screen when a crop is rejected. The original bytes are
    still in the PDF, so nothing needs to have been kept on disk.
    """
    doc = fitz.open(result.pdf_path)
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


def process_pdf(pdf_path, output_dir=None, on_page=None, should_stop=None):
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
                                     os.path.getsize(path))
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
