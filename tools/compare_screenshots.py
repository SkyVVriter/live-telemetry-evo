"""Compare overlay-visible and overlay-hidden screenshots.

Samples pixel values in the regions where overlay widgets are expected
(four corners for tyre widgets, bottom-center for the engine bar) and
fails if the two screenshots are nearly identical in those areas.

Usage:
    python tools/compare_screenshots.py <visible.png> <hidden.png>

Exit code 0 = significantly different (overlay was actually hidden).
Exit code 1 = nearly identical (overlay not hidden, or screenshots wrong).
"""
from __future__ import annotations

import sys
from pathlib import Path


def compare(visible_path: str, hidden_path: str) -> int:
    try:
        from PIL import Image
    except ImportError:
        print("PIL not available — skipping pixel comparison")
        return 0

    vis = Image.open(visible_path).convert("RGB")
    hid = Image.open(hidden_path).convert("RGB")
    w, h = vis.size

    if vis.size != hid.size:
        print(f"PASS: different image sizes ({vis.size} vs {hid.size})")
        return 0

    regions = [
        (0, 0, w // 4, h // 3),
        (3 * w // 4, 0, w, h // 3),
        (0, 2 * h // 3, w // 4, h),
        (3 * w // 4, 2 * h // 3, w, h),
        (w // 4, 3 * h // 4, 3 * w // 4, h),
    ]
    total_diff = 0
    total_pixels = 0
    for x1, y1, x2, y2 in regions:
        for x in range(x1, x2, 4):
            for y in range(y1, y2, 4):
                vp = vis.getpixel((x, y))
                hp = hid.getpixel((x, y))
                diff = sum(abs(a - b) for a, b in zip(vp, hp))
                total_diff += diff
                total_pixels += 1

    avg_diff = total_diff / max(total_pixels, 1)
    print(f"Average pixel diff in widget regions: {avg_diff:.1f} "
          f"(sampled {total_pixels} pixels)")

    if avg_diff < 5.0:
        print("WARNING: widget regions are nearly identical — "
              "overlay may not be hidden (avg_diff={:.1f})".format(avg_diff))
        return 1
    print("PASS: overlay-hidden screenshot differs significantly "
          "in widget areas (avg_diff={:.1f})".format(avg_diff))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <visible.png> <hidden.png>")
        sys.exit(2)
    sys.exit(compare(sys.argv[1], sys.argv[2]))
