"""lessonbook_visual_assets — local crop / annotation / masking tooling (PIL only).

Executor for the Claude-authored `lessonbook_visual_asset_plan`: it crops source
page images, draws teaching annotations (rectangle / arrow / label / dim-mask),
builds masked variants and side-by-side comparisons, and (only when the plan asks)
draws simple generated teaching diagrams. It writes a `lessonbook_visual_asset_manifest`.

Doctrine: **Python decides NO educational meaning.** It only realizes the visual plan
(which region, which annotation, which caption) that Claude authored. No OCR-only
reasoning, no external AI, no network. PIL (Pillow) only; numpy not required.

Annotation coordinates are NORMALIZED (0..1) in the target image's own space:
- for crop / annotated_crop / masked_variant: normalized within the *cropped* image
  (crop happens first, then annotations apply to the crop);
- for full_slide_anchor annotations: normalized within the full page.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
_ADAPTED_TYPES = {"annotated_crop", "masked_variant", "generated_teaching_diagram", "side_by_side"}
_GEN_BY_TYPE = {
    "full_slide_anchor": "source_crop", "crop": "source_crop",
    "annotated_crop": "python_annotation", "masked_variant": "python_annotation",
    "side_by_side": "python_annotation", "generated_teaching_diagram": "generated_diagram",
}


def _font(size: int):
    for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def page_image_path(run_dir: Path, source_page: int) -> Path:
    return Path(run_dir) / "extracted" / "pages" / f"page_{int(source_page):03d}.png"


def _to_px(box: List[float], size: Tuple[int, int], normalized: bool) -> Tuple[int, int, int, int]:
    W, H = size
    if normalized:
        x0, y0, x1, y1 = box
        return (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
    return tuple(int(v) for v in box)  # type: ignore


def _crop(img: Image.Image, box: List[float], normalized: bool = True) -> Image.Image:
    return img.crop(_to_px(box, img.size, normalized))


def _maybe_upscale(img: Image.Image, min_width: int = 900) -> Image.Image:
    w, h = img.size
    if w < min_width:
        scale = min_width / float(w)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _maybe_downscale(img: Image.Image, max_width: int = 1400) -> Image.Image:
    w, h = img.size
    if w > max_width:
        scale = max_width / float(w)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _arrowhead(d: ImageDraw.ImageDraw, p0, p1, color, width: int) -> None:
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    L = max(10, width * 4)
    for s in (math.radians(150), math.radians(-150)):
        x = p1[0] + L * math.cos(ang + s)
        y = p1[1] + L * math.sin(ang + s)
        d.line([p1, (x, y)], fill=color + (255,), width=width)


def _apply_annotations(img: Image.Image, annotations: List[Dict[str, Any]]) -> Image.Image:
    """Apply teaching annotations (coords normalized to img). Returns RGB image."""
    base = img.convert("RGBA")
    W, H = base.size

    def px(pt):
        return (pt[0] * W, pt[1] * H)

    # 1) dim/mask: veil the whole image, restore the kept regions from the original
    keeps = [a for a in annotations if a.get("type") == "dim_keep"]
    if keeps:
        veil_alpha = int(keeps[0].get("veil_alpha", 165))
        veiled = Image.alpha_composite(base, Image.new("RGBA", (W, H), (255, 255, 255, veil_alpha)))
        for a in keeps:
            for box in a.get("boxes", []):
                bb = _to_px(box, (W, H), True)
                veiled.paste(base.crop(bb), bb)
        base = veiled

    # 2) overlays: rectangles, arrows, labels
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for a in annotations:
        t = a.get("type")
        color = tuple(a.get("color", [214, 40, 40]))
        wdt = int(a.get("width", max(3, W // 260)))
        if t == "rectangle":
            x0, y0, x1, y1 = a["box"]
            d.rectangle([px((x0, y0)), px((x1, y1))], outline=color + (255,), width=wdt)
        elif t == "arrow":
            p0, p1 = px(a["from"]), px(a["to"])
            d.line([p0, p1], fill=color + (255,), width=wdt)
            _arrowhead(d, p0, p1, color, wdt)
        elif t == "label":
            xy = px(a.get("at", [0.02, 0.02]))
            txt = str(a.get("text", ""))
            f = _font(int(a.get("font", max(16, W // 45))))
            try:
                bb = d.textbbox(xy, txt, font=f)
                d.rectangle([bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2], fill=(255, 255, 255, 230))
            except Exception:
                pass
            d.text(xy, txt, fill=color + (255,), font=f)
    return Image.alpha_composite(base, overlay).convert("RGB")


def _generate_diagram(spec: Dict[str, Any]) -> Image.Image:
    """Draw a simple teaching diagram from a Claude-authored spec (axes / line / arrow /
    points / labels). Deterministic; no inference. Used only when the plan requests it."""
    W, H = int(spec.get("width", 900)), int(spec.get("height", 640))
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f = _font(int(spec.get("font", 22)))

    def px(pt):
        return (pt[0] * W, pt[1] * H)
    if spec.get("axes", True):
        d.line([px((0.08, 0.5)), px((0.96, 0.5))], fill=(60, 60, 60), width=2)  # x-ish
        d.line([px((0.12, 0.05)), px((0.12, 0.95))], fill=(60, 60, 60), width=2)  # y-ish
    for ln in spec.get("lines", []):
        d.line([px(ln["from"]), px(ln["to"])], fill=tuple(ln.get("color", [214, 40, 40])), width=int(ln.get("width", 4)))
    for ar in spec.get("arrows", []):
        p0, p1 = px(ar["from"]), px(ar["to"])
        col = tuple(ar.get("color", [20, 140, 60]))
        d.line([p0, p1], fill=col, width=int(ar.get("width", 4)))
        _ad = ImageDraw.Draw(img)
        ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
        for s in (math.radians(150), math.radians(-150)):
            d.line([p1, (p1[0] + 16 * math.cos(ang + s), p1[1] + 16 * math.sin(ang + s))], fill=col, width=int(ar.get("width", 4)))
    for pt in spec.get("points", []):
        x, y = px(pt["at"]); r = int(pt.get("r", 6))
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(pt.get("color", [30, 80, 200])))
    for lb in spec.get("labels", []):
        d.text(px(lb["at"]), str(lb.get("text", "")), fill=tuple(lb.get("color", [20, 20, 20])), font=f)
    return img


def _side_by_side(images: List[Image.Image], pad: int = 16, bg=(255, 255, 255)) -> Image.Image:
    h = max(im.height for im in images)
    imgs = [im.resize((int(im.width * h / im.height), h)) for im in images]
    W = sum(im.width for im in imgs) + pad * (len(imgs) + 1)
    canvas = Image.new("RGB", (W, h + 2 * pad), bg)
    x = pad
    for im in imgs:
        canvas.paste(im, (x, pad)); x += im.width + pad
    return canvas


def build_one(asset: Dict[str, Any], run_dir: Path) -> Image.Image:
    atype = asset["asset_type"]
    normalized = bool(asset.get("crop_norm", True))
    if atype == "generated_teaching_diagram":
        return _generate_diagram(asset.get("diagram_spec", {}))
    if atype == "side_by_side":
        parts = []
        for ref in asset.get("compose", []):
            im = Image.open(page_image_path(run_dir, ref["source_page"])).convert("RGB")
            if ref.get("crop_box"):
                im = _crop(im, ref["crop_box"], normalized)
            if ref.get("annotations"):
                im = _apply_annotations(im, ref["annotations"])
            parts.append(im)
        return _side_by_side(parts)
    img = Image.open(page_image_path(run_dir, asset["source_page"])).convert("RGB")
    if atype == "full_slide_anchor":
        return _maybe_downscale(img, int(asset.get("max_width", 1400)))
    if asset.get("crop_box"):
        img = _crop(img, asset["crop_box"], normalized)
    if atype in ("annotated_crop", "masked_variant"):
        img = _apply_annotations(img, asset.get("annotations", []))
    return _maybe_upscale(img, int(asset.get("min_width", 900)))


def build_assets_from_plan(plan: Dict[str, Any], run_dir, out_dir, *, manifest_run_rel: bool = True) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_assets: List[Dict[str, Any]] = []
    for asset in plan.get("assets", []):
        aid = asset["asset_id"]
        img = build_one(asset, run_dir)
        out_path = out_dir / f"{aid}.png"
        img.save(out_path)
        rel = str(out_path.relative_to(run_dir)).replace("\\", "/") if manifest_run_rel else str(out_path)
        ann_types = sorted({a.get("type") for a in asset.get("annotations", []) if a.get("type")})
        src_page = asset.get("source_page")
        if src_page is None and asset.get("compose"):
            src_page = asset["compose"][0].get("source_page")
        entry = {
            "asset_id": aid,
            "path": rel,
            "source_page": src_page,
            "asset_type": asset["asset_type"],
            "annotations_applied": ann_types,
            "caption_fa": asset.get("caption_fa", ""),
            "generation_method": asset.get("generation_method", _GEN_BY_TYPE.get(asset["asset_type"], "source_crop")),
            "is_adapted_teaching_visual": asset["asset_type"] in _ADAPTED_TYPES,
            "qa_status": "pass" if (img.size[0] > 0 and img.size[1] > 0) else "fail",
            "used_in_pdf": bool(asset.get("required", True)),
        }
        if asset.get("crop_box"):
            entry["crop_box"] = asset["crop_box"]
        manifest_assets.append(entry)
    return {
        "schema_version": "1.0.0",
        "run_id": plan.get("run_id", ""),
        "package_id": plan.get("package_id", ""),
        "provenance": {"authored_by": "python_executor"},
        "assets": manifest_assets,
    }


def smoke_assets(run_dir, out_dir) -> Dict[str, Any]:
    """Part B smoke: full anchor + formula crop + figure crop + annotated highlight from page 6."""
    plan = {
        "schema_version": "1.0.0", "run_id": "smoke", "package_id": "smoke",
        "provenance": {"authored_by": "CLAUDE_CODE_MANUAL_AGENT"},
        "assets": [
            {"asset_id": "smoke_p6_full", "source_page": 6, "asset_type": "full_slide_anchor",
             "teaching_purpose": "trace anchor", "caption_fa": "اسلاید اصلیِ صفحهٔ ۶ (مرجع)", "required": True,
             "generation_method": "source_crop"},
            {"asset_id": "smoke_p6_formula", "source_page": 6, "asset_type": "crop",
             "crop_box": [0.08, 0.17, 0.72, 0.26], "min_width": 900,
             "teaching_purpose": "show f(x)=<w,x>+b clearly", "caption_fa": "تعریفِ تابعِ تفکیکِ خطی", "required": True,
             "generation_method": "source_crop"},
            {"asset_id": "smoke_p6_figure", "source_page": 6, "asset_type": "crop",
             "crop_box": [0.04, 0.34, 0.46, 0.86], "min_width": 700,
             "teaching_purpose": "the geometry figure", "caption_fa": "نمودارِ هندسیِ مرز و بردارِ وزن", "required": True,
             "generation_method": "source_crop"},
            {"asset_id": "smoke_p6_annot", "source_page": 6, "asset_type": "annotated_crop",
             "crop_box": [0.04, 0.34, 0.46, 0.86], "min_width": 700,
             "annotations": [
                 {"type": "rectangle", "box": [0.30, 0.10, 0.62, 0.55], "color": [20, 140, 60], "width": 4},
                 {"type": "label", "at": [0.30, 0.02], "text": "w (normal)", "color": [20, 140, 60]},
                 {"type": "arrow", "from": [0.85, 0.15], "to": [0.62, 0.40], "color": [214, 40, 40]},
                 {"type": "label", "at": [0.66, 0.06], "text": "boundary f=0", "color": [214, 40, 40]}],
             "teaching_purpose": "highlight w arrow + red boundary", "caption_fa": "برجسته‌سازیِ بردارِ $w$ و مرزِ قرمز", "required": True,
             "generation_method": "python_annotation"},
        ],
    }
    return build_assets_from_plan(plan, run_dir, out_dir)
