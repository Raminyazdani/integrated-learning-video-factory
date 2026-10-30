"""source_crop - crop / zoom / highlight original PDF page screenshots.

Used by the slide builder to make dense source regions (formulas, theorems,
figures) readable inside a teaching video, while keeping a small full-page
context thumbnail. All boxes are NORMALIZED (0..1) in source-page coordinates
so crops are run-specific (authored in source_crops/source_crop_manifest.json),
never hardcoded globally.

Highlights are drawn as thin OUTLINE rectangles (no fill) so they never obscure
source text.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

try:
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None

CROP_MODES = {
    "full_page_context",
    "title_region",
    "formula_region",
    "figure_region",
    "paragraph_region",
    "crop_with_highlight",
    "context_plus_zoom",
}

# restrained, template-consistent highlight color (muted red-orange)
_HL_COLOR = (200, 60, 40)
_HL_WIDTH_FRAC = 0.006  # outline width as a fraction of image min-dimension


def _px_box(w: int, h: int, box_norm) -> Tuple[int, int, int, int]:
    x0, y0, x1, y1 = box_norm
    x0 = max(0.0, min(1.0, x0)); y0 = max(0.0, min(1.0, y0))
    x1 = max(0.0, min(1.0, x1)); y1 = max(0.0, min(1.0, y1))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)


def render_zoom(page_path, box_norm, out_path, *, highlight_sub_norm: Optional[tuple] = None):
    """Crop the normalized box from the page and save it. Optionally draw a thin
    outline highlight (sub-box given in FULL-PAGE normalized coords). Returns
    (width_px, height_px) of the saved crop, or None on failure."""
    if Image is None:
        return None
    page_path = Path(page_path)
    with Image.open(page_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        l, t, r, b = _px_box(w, h, box_norm)
        if r - l < 4 or b - t < 4:
            l, t, r, b = 0, 0, w, h
        crop = im.crop((l, t, r, b))
        if highlight_sub_norm is not None:
            cw, ch = crop.size
            hl, ht, hr, hb = _px_box(w, h, highlight_sub_norm)
            # translate to crop-local coords
            hl, hr = hl - l, hr - l
            ht, hb = ht - t, hb - t
            hl = max(0, min(cw - 1, hl)); hr = max(0, min(cw - 1, hr))
            ht = max(0, min(ch - 1, ht)); hb = max(0, min(ch - 1, hb))
            if hr - hl > 3 and hb - ht > 3:
                d = ImageDraw.Draw(crop)
                lw = max(2, int(min(cw, ch) * _HL_WIDTH_FRAC))
                d.rectangle([hl, ht, hr, hb], outline=_HL_COLOR, width=lw)
        crop.save(str(out_path))
        return crop.size


def render_thumbnail(page_path, box_norm, out_path, *, max_w: int = 700):
    """Downscale the whole page to a thumbnail and draw a thin outline rectangle
    showing where the crop came from (full-page context). Returns thumb size."""
    if Image is None:
        return None
    with Image.open(Path(page_path)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_w / float(w))
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        thumb = im.resize((tw, th))
        l, t, r, b = _px_box(tw, th, box_norm)
        if r - l > 2 and b - t > 2:
            d = ImageDraw.Draw(thumb)
            lw = max(2, int(min(tw, th) * 0.012))
            d.rectangle([l, t, r, b], outline=_HL_COLOR, width=lw)
        thumb.save(str(out_path))
        return thumb.size
