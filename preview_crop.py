"""
preview_crop.py -- run the cropper over some PDFs and write the results to disk
so you can look at them before committing a batch to the API.

    python preview_crop.py <pdf-or-directory> [more...] [-o OUTPUT_DIR]

For every page it writes three files into OUTPUT_DIR (default output/crop_preview):

    <name>_<nn>_cropped.png     what the pipeline would actually upload
    <name>_<nn>_original.png    the raster as extracted, for comparison
    <name>_<nn>_review.png      both side by side, contrast-stretched

The review image is the one to look at. Provider mount board and scanned paper
are both near-white and hard to tell apart on screen, so the review image
stretches the 222-255 range: the mount turns bright, the paper turns grey, and
the red/green boxes show what the old and new croppers each decided.

A summary table is printed at the end. The column to watch is "trimmed", which
reports pixels removed from each side as left/top/right/bottom.
"""

import argparse
import hashlib
import os
import sys

import cv2
import fitz
import numpy as np

from crop_core import detect_scan_box

DEFAULT_OUT = os.path.join("output", "crop_preview")
STRETCH_LO, STRETCH_HI = 222, 255


def legacy_box(cv_img):
    """The original threshold-250 + closing + largest-contour rule, for comparison."""
    height, width = cv_img.shape[:2]
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 250, 255, cv2.THRESH_BINARY_INV)
    kx, ky = max(5, int(width * 0.02)), max(5, int(height * 0.02))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    closed = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return (0, 0, width, height)
    best = max(contours, key=lambda c: (lambda r: r[2] * r[3])(cv2.boundingRect(c)))
    x, y, w, h = cv2.boundingRect(best)
    return (x, y, x + w, y + h)


def stretch(gray):
    """Expose near-white mount against near-white paper."""
    span = STRETCH_HI - STRETCH_LO
    return np.clip((gray.astype(np.float32) - STRETCH_LO) / span * 255, 0, 255).astype(np.uint8)


def review_image(cv_img, legacy, new, target_h=1000):
    """Original with both boxes drawn, beside the actual cropped result."""
    H, W = cv_img.shape[:2]
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    left = cv2.cvtColor(stretch(gray), cv2.COLOR_GRAY2BGR)
    thickness = max(3, W // 250)
    cv2.rectangle(left, (legacy[0], legacy[1]), (legacy[2] - 1, legacy[3] - 1),
                  (0, 0, 255), thickness)
    if new:
        cv2.rectangle(left, (new[0], new[1]), (new[2] - 1, new[3] - 1),
                      (0, 200, 0), thickness)

    if new:
        l, t, r, b = new
        right = cv2.cvtColor(stretch(gray[t:b, l:r]), cv2.COLOR_GRAY2BGR)
    else:
        right = cv2.cvtColor(stretch(gray), cv2.COLOR_GRAY2BGR)

    def fit(img):
        h, w = img.shape[:2]
        return cv2.resize(img, (max(1, int(w * target_h / h)), target_h))

    left, right = fit(left), fit(right)
    gap = np.full((target_h, 24, 3), 255, np.uint8)
    return np.hstack([left, gap, right])


def iter_pdfs(paths):
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(".pdf"):
                    yield os.path.join(p, name)
        elif p.lower().endswith(".pdf"):
            yield p
        else:
            print(f"[!] not a PDF or directory, skipping: {p}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="PDF files or directories of PDFs")
    ap.add_argument("-o", "--output-dir", default=DEFAULT_OUT)
    ap.add_argument("--no-review", action="store_true",
                    help="skip the side-by-side review images")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for pdf_path in iter_pdfs(args.inputs):
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            print(f"[!] could not open {pdf_path}: {e}")
            continue

        counter = 1
        seen = set()
        for page_index in range(len(doc)):
            # Same extraction the pipeline uses: dedupe identical rasters by MD5.
            for info in sorted(doc[page_index].get_images(full=True), key=lambda i: i[7]):
                blob = doc.extract_image(info[0])
                data = blob["image"]
                digest = hashlib.md5(data).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)

                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    print(f"[!] could not decode image {counter} in {base}")
                    continue

                H, W = img.shape[:2]
                new = detect_scan_box(img)
                old = legacy_box(img)
                l, t, r, b = new if new else (0, 0, W, H)

                stem = os.path.join(out_dir, f"{base}_{counter:03d}")
                cv2.imwrite(f"{stem}_original.png", img)
                cv2.imwrite(f"{stem}_cropped.png", img[t:b, l:r])
                if not args.no_review:
                    cv2.imwrite(f"{stem}_review.png", review_image(img, old, new))

                rows.append((f"{base[:20]}_{counter:03d}", f"{W}x{H}",
                             "no crop" if old == (0, 0, W, H) else "crops",
                             f"{l}/{t}/{W-r}/{H-b}" if new else "no crop",
                             (r - l) * (b - t) / float(W * H)))
                counter += 1
        doc.close()

    if not rows:
        print("No page images found.")
        return 1

    print(f"\n{'page':26}{'raster':>12}{'legacy':>10}{'new: trimmed L/T/R/B':>24}{'keeps':>9}")
    print("-" * 81)
    for name, size, old, trimmed, keep in rows:
        print(f"{name:26}{size:>12}{old:>10}{trimmed:>24}{keep:>8.1%}")
    print("-" * 81)
    print(f"{len(rows)} pages written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
