"""
test_crop.py -- regression harness for the page-scan cropper.

Run this after changing crop_core.py:

    python test_crop.py

It builds test images from real scans (so the paper grain and JPEG artefacts
are genuine), mounts them on synthetic provider backgrounds of known geometry,
and scores each cropper against the known-correct box.

The number that matters is "over-crops": a crop that cut into the page. Those
lose text permanently and silently. Under-cropping only wastes a few tokens.

Point SAMPLE_PDFS at any handful of provider PDFs you have locally.
"""

import os
import sys
import time

import cv2
import fitz
import numpy as np

from crop_core import detect_scan_box

SAMPLE_PDFS = [
    r"C:\Users\Daniel\Downloads\Untitled_item.pdf",
    r"C:\Users\Daniel\Downloads\DESERTIONS_FROM_THE_BRITISH_AR.pdf",
    r"C:\Users\Daniel\Downloads\The_Mischief_of_Pensions.pdf",
]


def legacy_box(cv_img):
    """The original threshold-250 + morphological-closing + largest-contour rule."""
    height, width = cv_img.shape[:2]
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 250, 255, cv2.THRESH_BINARY_INV)
    kx, ky = max(5, int(width * 0.02)), max(5, int(height * 0.02))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return (0, 0, width, height)
    best = max(contours, key=lambda c: (lambda r: r[2] * r[3])(cv2.boundingRect(c)))
    x, y, w, h = cv2.boundingRect(best)
    return (x, y, x + w, y + h)


def load_page_images(pdf_path):
    doc = fitz.open(pdf_path)
    out = []
    for page_index in range(len(doc)):
        for info in doc[page_index].get_images(full=True):
            blob = doc.extract_image(info[0])
            img = cv2.imdecode(np.frombuffer(blob["image"], np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append(img)
    doc.close()
    return out


def mount(scan, bg_value, pad_frac=0.05, noise=1):
    """Place a scan on a flat provider-style background of known geometry."""
    h, w = scan.shape[:2]
    py, px = int(h * pad_frac), int(w * pad_frac)
    canvas = np.full((h + 2 * py, w + 2 * px, 3), bg_value, np.int16)
    canvas += np.random.normal(0, noise, canvas.shape).astype(np.int16)
    canvas = np.clip(canvas, 0, 255).astype(np.uint8)
    canvas[py:py + h, px:px + w] = scan
    return canvas, (px, py, px + w, py + h)


def blank_paper(scan, hh, ww):
    """Tile a genuinely blank patch of a real margin to synthesise empty paper."""
    h, w = scan.shape[:2]
    patch = scan[int(h * 0.02):int(h * 0.06), int(w * 0.02):int(w * 0.06)]
    reps = (hh // patch.shape[0] + 1, ww // patch.shape[1] + 1, 1)
    return np.tile(patch, reps)[:hh, :ww]


def make_page(scan, content_frac, gain):
    """A real scan, brightened by `gain`, with its lower part blanked out."""
    page = np.clip(scan.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    h = page.shape[0]
    cut = int(h * content_frac)
    if cut < h:
        page[cut:] = blank_paper(page, h - cut, page.shape[1])
    return page


def score(truth, box, width, height):
    """Returns (fraction of the true scan retained, IoU)."""
    if box is None:
        box = (0, 0, width, height)
    ix1, iy1 = max(truth[0], box[0]), max(truth[1], box[1])
    ix2, iy2 = min(truth[2], box[2]), min(truth[3], box[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_t = (truth[2] - truth[0]) * (truth[3] - truth[1])
    area_b = (box[2] - box[0]) * (box[3] - box[1])
    return inter / area_t, inter / (area_t + area_b - inter)


def deband(scan):
    """
    Strip the provider's watermark band off a real scan so it can be used as
    clean ground truth. Without this the synthetic cases would carry a band
    inside their "true" box and a correct crop would score as an over-crop.
    """
    box = detect_scan_box(scan)
    if box is None:
        return scan
    l, t, r, b = box
    return scan[t:b, l:r]


def build_cases():
    cases = []
    for path in SAMPLE_PDFS:
        if not os.path.exists(path):
            print(f"[!] skipping missing sample: {path}")
            continue
        tag = os.path.basename(path)[:12]
        scans = load_page_images(path)
        if not scans:
            continue
        base = deband(scans[0])

        # Mounted pages, with known ground truth.
        for content_frac, cname in ((1.0, "full page"), (0.45, "half-empty"), (0.15, "title page")):
            for gain in (1.00, 1.06, 1.12):
                for bg, bname in ((255, "white mount"), (247, "near-white"), (238, "grey mount")):
                    np.random.seed(2)
                    page = make_page(base, content_frac, gain)
                    img, truth = mount(page, bg)
                    cases.append((f"{tag} {cname:11} gain{gain:.2f} {bname:11}", img, truth))
    return cases


def report_real_samples():
    """
    The real rasters have no verified ground-truth box, so this reports what
    each cropper trims rather than scoring it. Eyeball these numbers: the
    provider band is ~2-3% off the bottom, and nothing should move much else.
    """
    print("\nReal sample rasters -- pixels trimmed per side (left/top/right/bottom)")
    for path in SAMPLE_PDFS:
        if not os.path.exists(path):
            continue
        tag = os.path.basename(path)[:12]
        for i, img in enumerate(load_page_images(path)[:2]):
            h, w = img.shape[:2]
            out = []
            for name, fn in (("legacy", legacy_box), ("new", detect_scan_box)):
                box = fn(img)
                if box is None or box == (0, 0, w, h):
                    out.append(f"{name}: no crop")
                else:
                    l, t, r, b = box
                    out.append(f"{name}: {l}/{t}/{w-r}/{h-b}"
                               f" (keeps {(r-l)*(b-t)/(w*h):.1%})")
            print(f"  {tag} page{i} {w}x{h:<5}  " + "   ".join(out))


def evaluate(name, fn, cases):
    retained, ious, overcrops = [], [], []
    start = time.time()
    for label, img, truth in cases:
        h, w = img.shape[:2]
        r, i = score(truth, fn(img), w, h)
        retained.append(r)
        ious.append(i)
        if r < 0.99:
            overcrops.append((r, label))
    elapsed = time.time() - start
    print(f"\n{name}")
    print(f"  mean scan retained : {np.mean(retained):6.1%}")
    print(f"  mean IoU           : {np.mean(ious):6.3f}   (1.0 = perfectly tight)")
    print(f"  OVER-CROPS         : {len(overcrops)}/{len(cases)}")
    print(f"  worst case         : {min(retained):6.1%} of the scan retained")
    print(f"  time               : {elapsed:.1f}s")
    for r, label in sorted(overcrops)[:8]:
        print(f"     over-cropped: {label}  retained {r:.1%}")
    return len(overcrops), min(retained)


def main():
    cases = build_cases()
    if not cases:
        print("No sample PDFs found. Edit SAMPLE_PDFS at the top of this file.")
        return 1
    print(f"Built {len(cases)} test images from {len(SAMPLE_PDFS)} sample PDFs.")
    evaluate("LEGACY  (threshold 250 + largest contour)", legacy_box, cases)
    bad, worst = evaluate("CURRENT (crop_core.detect_scan_box)", detect_scan_box, cases)
    report_real_samples()

    print()
    if bad == 0:
        print("PASS: no over-crops.")
        return 0
    if worst >= 0.98:
        print(f"PASS: {bad} marginal over-crop(s), all retaining >=98% of the scan.")
        return 0
    print(f"FAIL: {bad} over-crop(s), worst retained only {worst:.1%}.")
    print("      Try raising PAD_FRAC in crop_core.py.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
