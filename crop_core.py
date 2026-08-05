"""
crop_core.py -- locate the page scan inside a provider-mounted image.

The image provider mounts each page scan on a flat white board and stamps a
copyright line into it (on the ProQuest samples this is a ~80-95px pure-white
band across the bottom, plus a thin and sometimes absent margin on the other
three sides). We want to send the API the scan and nothing else.

Design note -- why this keys off the PAPER tone, not the mount tone:

The obvious approach is to model the mount from the outer border of the image
and flood inward. That fails on this material, because the side margins are
often only a few pixels wide or missing entirely, so the border is mostly
paper: the model inverts and the detector either declines or eats the page.

What is stable across the samples is the *contrast* between the two. Scanned
paper sits at 230-241; the provider's board is a flat 255. So we measure the
paper tone from the interior of the image and treat anything meaningfully
BRIGHTER than paper as mount. Ink, gutter shadows and dark plate edges are all
darker than paper, so they are never mistaken for board.

The expensive failure is OVER-cropping: a crop that cuts into the page loses
text permanently and nothing downstream can detect it. This module is
deliberately biased toward under-cropping, and returns None whenever it is not
confident, meaning "keep the full image".

Public API:
    detect_scan_box(cv_img) -> (left, top, right, bottom) or None
"""

import cv2
import numpy as np

# --- Tunables -------------------------------------------------------------

WORK_MAX_SIDE    = 1200   # analyse at this resolution; ~5x faster, less noise
MOUNT_MARGIN     = 8      # mount must be at least this much brighter than paper
MOUNT_MAD_K      = 3.0    # ...or this many MADs of the paper tone, whichever is more
CUTOFF_CEILING   = 252    # never demand more than this to call a pixel mount
PAPER_TOO_BRIGHT = 246    # above this the paper is white; no separation to exploit
COVERAGE         = 0.60   # a row/col is "scan" when >=60% of it is non-mount
COVERAGE_WEAK    = 0.15   # hysteresis threshold for growing the run outward
PAD_FRAC         = 0.004  # padding added back around the box. Raise this if you
                          # ever see a shaved edge; cheapest, safest knob here.
MIN_KEEP_AREA    = 0.35   # refuse crops that would discard most of the frame
MAX_ASPECT_DRIFT = 2.5    # refuse crops that change the aspect ratio wildly
NO_CROP_BELOW    = 0.004  # ignore crops that would trim less than 0.4% per side


def _paper_tone(gray):
    """
    Median and MAD of the page paper, measured from the interior.

    The interior is mostly paper plus ink. Ink is dark and a minority, so the
    median lands on paper; the MAD is taken against only the non-dark pixels so
    that heavy text does not inflate it.
    """
    h, w = gray.shape
    interior = gray[int(h * 0.20):int(h * 0.80), int(w * 0.20):int(w * 0.80)]
    med = float(np.median(interior))
    light = interior[interior >= med - 25]      # drop ink before measuring spread
    mad = float(np.median(np.abs(light - med))) if light.size else 0.0
    return med, mad


def _largest_run(mask_1d, min_len):
    """Longest contiguous True run in a 1-D boolean mask, or None."""
    edges = np.flatnonzero(
        np.diff(np.concatenate(([0], mask_1d.view(np.int8), [0])))
    )
    if edges.size == 0:
        return None
    starts, ends = edges[0::2], edges[1::2]
    lengths = ends - starts
    if lengths.size == 0 or lengths.max() < min_len:
        return None
    i = int(np.argmax(lengths))
    return int(starts[i]), int(ends[i])


def _grow(run, coverage):
    """Extend a seed run outward while coverage stays above the weak threshold."""
    start, end = run
    while start > 0 and coverage[start - 1] >= COVERAGE_WEAK:
        start -= 1
    while end < len(coverage) and coverage[end] >= COVERAGE_WEAK:
        end += 1
    return start, end


def detect_scan_box(cv_img, debug=False):
    """
    Locate the mounted page scan in a BGR OpenCV image.

    Returns (left, top, right, bottom) in full-resolution pixel coordinates, or
    None when no crop should be applied. None is a normal, safe result: it
    means "keep the whole image", not "an error occurred".
    """
    H, W = cv_img.shape[:2]
    if H < 50 or W < 50:
        return None

    scale = min(1.0, WORK_MAX_SIDE / max(H, W))
    small = (cv2.resize(cv_img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
             if scale < 1.0 else cv_img)
    gray = cv2.medianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 3)
    h, w = gray.shape

    # 1. Measure the paper, then set the mount cutoff above it.
    paper_med, paper_mad = _paper_tone(gray)

    # If the paper itself is white there is no tonal separation to exploit.
    # Decline rather than guess.
    if paper_med > PAPER_TOO_BRIGHT:
        if debug:
            print(f"    [no crop] paper too bright to separate (paper={paper_med:.0f})")
        return None

    # Grainy paper needs a wider band, but the cutoff is capped: the board is a
    # flat 255, so demanding more than CUTOFF_CEILING only loses real mount.
    cutoff = min(paper_med + max(MOUNT_MARGIN, MOUNT_MAD_K * paper_mad),
                 CUTOFF_CEILING)

    # 2. Everything not brighter than the cutoff is scan. Ink, shadows and
    #    plate edges are darker than paper, so they land on the right side.
    content = gray <= cutoff

    # 3. Reduce to row/column coverage profiles. The scan is a solid rectangle,
    #    so its rows and columns are almost entirely non-mount. A block of text
    #    is not, which is what stops this mistaking a page margin for mount the
    #    way a "largest contour" rule does. The watermark band is sparse text on
    #    white, so its rows fall well under the coverage threshold.
    row_cov = content.mean(axis=1)
    col_cov = content.mean(axis=0)
    rows = _largest_run(row_cov >= COVERAGE, int(h * 0.20))
    cols = _largest_run(col_cov >= COVERAGE, int(w * 0.20))
    if rows is None or cols is None:
        if debug:
            print("    [no crop] no confident row/column run")
        return None

    # Hysteresis, so sparse leading/trailing content (a title page, a near-blank
    # verso, a running head) is not shaved off the ends.
    top, bottom = _grow(rows, row_cov)
    left, right = _grow(cols, col_cov)

    # 4. Pad, scale back up, then apply the safety guards.
    pad_x, pad_y = int(w * PAD_FRAC), int(h * PAD_FRAC)
    left, top = max(0, left - pad_x), max(0, top - pad_y)
    right, bottom = min(w, right + pad_x), min(h, bottom + pad_y)

    inv = 1.0 / scale
    L, T = int(round(left * inv)), int(round(top * inv))
    R, B = min(W, int(round(right * inv))), min(H, int(round(bottom * inv)))
    if R - L < 10 or B - T < 10:
        return None

    keep = ((R - L) * (B - T)) / float(W * H)
    if keep < MIN_KEEP_AREA:
        if debug:
            print(f"    [no crop] would keep only {keep:.1%} of the frame")
        return None

    ar_before, ar_after = W / H, (R - L) / (B - T)
    if max(ar_after / ar_before, ar_before / ar_after) > MAX_ASPECT_DRIFT:
        if debug:
            print("    [no crop] aspect ratio drift")
        return None

    # Not worth a re-encode if every side moves less than a fraction of a percent.
    if (L <= W * NO_CROP_BELOW and T <= H * NO_CROP_BELOW
            and W - R <= W * NO_CROP_BELOW and H - B <= H * NO_CROP_BELOW):
        return None

    return (L, T, R, B)
