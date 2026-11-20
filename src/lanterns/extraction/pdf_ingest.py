"""Optical DPI triage, JBIG2 risk detection, and immutable 300-400 DPI scan rendering via PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image

from lanterns.core.hashes import sha256_bytes, sha256_file

INK_DARK_THRESHOLD = 175
BLANK_GLOBAL_INK_MAX = 0.0025
BLANK_LOCAL_TILE_MAX = 0.035


@dataclass(frozen=True)
class PagePreflight:
    page_number: int
    width_pt: float
    height_pt: float
    witness_text: str
    witness_char_count: int
    global_ink_ratio: float
    max_tile_ink_ratio: float
    is_blank_or_tissue: bool
    uses_jbig2: bool
    is_mrc_derivative: bool
    source_page_sha256: str
    render_px: tuple[int, int] = (0, 0)
    native_optical_dpi: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width_pt": round(self.width_pt, 2),
            "height_pt": round(self.height_pt, 2),
            "witness_char_count": self.witness_char_count,
            "global_ink_ratio": round(self.global_ink_ratio, 6),
            "max_tile_ink_ratio": round(self.max_tile_ink_ratio, 6),
            "is_blank_or_tissue": self.is_blank_or_tissue,
            "uses_jbig2": self.uses_jbig2,
            "is_mrc_derivative": self.is_mrc_derivative,
            "source_page_sha256": self.source_page_sha256,
            "render_px": list(self.render_px),
            "native_optical_dpi": round(self.native_optical_dpi, 1) if self.native_optical_dpi else None,
        }


def _top_layer_uses_jbig2(page: Any, xref: int) -> bool:
    """Return True when the visible scan layer or its soft mask uses lossy-capable JBIG2 compression."""
    if xref <= 0:
        return False
    doc = page.parent
    if doc.xref_get_key(xref, "Filter")[1] == "/JBIG2Decode":
        return True
    kind, smask = doc.xref_get_key(xref, "SMask")
    if kind != "xref":
        return False
    smask_xref = int(smask.split()[0])
    return doc.xref_get_key(smask_xref, "Filter")[1] == "/JBIG2Decode"


def _extract_spanning_images(page: Any) -> list[dict[str, Any]]:
    rect = page.rect
    page_area_pt = max(1.0, float(rect.width)) * max(1.0, float(rect.height))
    infos = page.get_image_info(xrefs=True) or []
    spanning: list[dict[str, Any]] = []
    for info in infos:
        iw, ih = int(info.get("width") or 0), int(info.get("height") or 0)
        bbox = info.get("bbox") or (0, 0, 0, 0)
        bw = max(0.0, float(bbox[2]) - float(bbox[0]))
        bh = max(0.0, float(bbox[3]) - float(bbox[1]))
        if iw > 0 and ih > 0 and ((bw * bh) / page_area_pt) >= 0.45:
            eff_long_in = max(bw, bh, 1.0) / 72.0
            spanning.append(
                {
                    "width_px": iw,
                    "height_px": ih,
                    "optical_dpi": max(iw, ih) / eff_long_in,
                    "bpc": int(info.get("bpc") or 8),
                    "xref": int(info.get("xref") or 0),
                }
            )
    return spanning


def inspect_page_optical_dpi(page: Any) -> dict[str, Any]:
    """Inspect embedded raster images on `page` for true optical DPI, MRC layers, and JBIG2 risk."""
    spanning = _extract_spanning_images(page)
    if not spanning:
        return {
            "has_full_page_scan": False,
            "native_long_px": None,
            "optical_dpi": None,
            "is_mrc_derivative": False,
            "is_bitonal_1bit": False,
            "uses_jbig2": False,
        }

    high_bpc = [s for s in spanning if s["bpc"] > 1]
    best = max(high_bpc or spanning, key=lambda s: s["width_px"] * s["height_px"])
    has_1bit_mask = any(s["bpc"] == 1 for s in spanning)
    low_res_bg = any(s["bpc"] > 1 and s["optical_dpi"] < 190 for s in spanning)
    top_sharp = spanning[-1]["bpc"] > 1 and spanning[-1]["optical_dpi"] >= 240
    is_mrc = len(spanning) >= 2 and (has_1bit_mask or low_res_bg) and not top_sharp

    return {
        "has_full_page_scan": True,
        "native_long_px": max(best["width_px"], best["height_px"]),
        "optical_dpi": round(float(best["optical_dpi"]), 1),
        "is_mrc_derivative": is_mrc,
        "is_bitonal_1bit": all(s["bpc"] == 1 for s in spanning),
        "uses_jbig2": _top_layer_uses_jbig2(page, spanning[-1]["xref"]),
    }


def compute_optimal_render_zoom(
    page: Any,
    *,
    dpi: int = 350,
    min_long_px: int = 2400,
    max_long_px: int = 3800,
) -> tuple[float, dict[str, Any]]:
    """Compute `(zoom, optical_info)` rendering scans crisply at 300-400 DPI without resampling blur."""
    clamped_dpi = max(150, min(450, int(dpi)))
    rect = page.rect
    pt_long = max(1.0, float(rect.width), float(rect.height))
    is_landscape = float(rect.width) > float(rect.height) * 1.12
    eff_max_px = max(max_long_px, 4200) if is_landscape else max_long_px

    opt = inspect_page_optical_dpi(page)
    native_long_px = opt.get("native_long_px")
    if native_long_px:
        if native_long_px >= min_long_px:
            target_long_px = min(float(eff_max_px), float(native_long_px))
        else:
            requested_px = pt_long * (clamped_dpi / 72.0)
            target_long_px = min(float(eff_max_px), max(float(native_long_px), requested_px))
        return max(0.25, target_long_px / pt_long), opt

    requested_px = pt_long * (clamped_dpi / 72.0)
    return max(0.25, min(float(eff_max_px), requested_px) / pt_long), opt


def _paper_dark_cutoff(gray_inner: Image.Image) -> int:
    hist = gray_inner.histogram()
    target = int((sum(hist) or 1) * 0.90)
    acc = 0
    p90 = 255
    for val, count in enumerate(hist):
        acc += count
        if acc >= target:
            p90 = val
            break
    return max(40, min(INK_DARK_THRESHOLD, p90 - 42))


def measure_ink_density(image: Image.Image, grid: int = 8) -> tuple[float, float]:
    """Return `(global_ink_ratio, max_grid_tile_ink_ratio)` after 5% frame trim."""
    gray = image.convert("L")
    w, h = gray.size
    tx, ty = int(w * 0.05), int(h * 0.05)
    inner = gray.crop((tx, ty, max(tx + 1, w - tx), max(ty + 1, h - ty)))
    iw, ih = inner.size
    dark_cutoff = _paper_dark_cutoff(inner)
    binary = inner.point(lambda v: 1 if v < dark_cutoff else 0)
    hist = binary.histogram()
    global_ink = (hist[1] / max(1, iw * ih)) if len(hist) > 1 else 0.0

    max_tile = 0.0
    tw, th = max(1, iw // grid), max(1, ih // grid)
    for gx in range(grid):
        for gy in range(grid):
            tile = binary.crop((gx * tw, gy * th, min(iw, (gx + 1) * tw), min(ih, (gy + 1) * th)))
            t_hist = tile.histogram()
            ratio = (t_hist[1] / max(1, tile.width * tile.height)) if len(t_hist) > 1 else 0.0
            if ratio > max_tile:
                max_tile = ratio
    return global_ink, max_tile


def _is_nearly_grayscale(img: Image.Image) -> bool:
    if img.mode == "L":
        return True
    thumb = img.convert("RGB").resize((min(120, img.width), min(120, img.height)), Image.Resampling.BILINEAR)
    raw = thumb.tobytes()
    total = len(raw) // 3
    if total == 0:
        return False
    colorful = 0
    for idx in range(0, len(raw), 3):
        if max(raw[idx], raw[idx + 1], raw[idx + 2]) - min(raw[idx], raw[idx + 1], raw[idx + 2]) > 12:
            colorful += 1
            if colorful > max(2, int(total * 0.015)):
                return False
    return True


def inspect_and_render_page(
    pdf_path: str | Path,
    page_number: int,
    out_png: str | Path,
    *,
    dpi: int = 350,
    grayscale_if_monochrome: bool = True,
    out_witness_txt: str | Path | None = None,
    doc: Any | None = None,
    reuse_existing: bool = True,
) -> PagePreflight:
    """Render 1-based `page_number` to immutable PNG, record `source_page_sha256`, and run optical preflight."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    owns_doc = doc is None
    active_doc = fitz.open(pdf_path) if owns_doc else doc
    try:
        if page_number < 1 or page_number > active_doc.page_count:
            raise IndexError(f"Page {page_number} out of range 1..{active_doc.page_count}")
        page = active_doc[page_number - 1]
        witness_text = page.get_text("text") or ""
        zoom, opt_info = compute_optimal_render_zoom(page, dpi=dpi)
        width_pt, height_pt = float(page.rect.width), float(page.rect.height)
        if reuse_existing and out_png.is_file() and out_png.stat().st_size > 0:
            png_bytes = out_png.read_bytes()
            render_dims = (0, 0)
        else:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pil_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if grayscale_if_monochrome and _is_nearly_grayscale(pil_img):
                pil_img = pil_img.convert("L")
            pil_img.save(out_png, format="PNG")
            png_bytes = out_png.read_bytes()
            render_dims = (int(pil_img.width), int(pil_img.height))
    finally:
        if owns_doc:
            active_doc.close()

    if out_witness_txt is not None:
        wpath = Path(out_witness_txt)
        wpath.parent.mkdir(parents=True, exist_ok=True)
        wpath.write_text(witness_text, encoding="utf-8")

    with Image.open(out_png) as img:
        if render_dims == (0, 0):
            render_dims = (int(img.width), int(img.height))
        global_ink, max_tile_ink = measure_ink_density(img)

    clean_witness_chars = sum(1 for ch in witness_text if not ch.isspace())
    is_blank = global_ink <= BLANK_GLOBAL_INK_MAX and max_tile_ink <= BLANK_LOCAL_TILE_MAX and clean_witness_chars < 12
    return PagePreflight(
        page_number=page_number,
        width_pt=width_pt,
        height_pt=height_pt,
        witness_text=witness_text,
        witness_char_count=clean_witness_chars,
        global_ink_ratio=global_ink,
        max_tile_ink_ratio=max_tile_ink,
        is_blank_or_tissue=is_blank,
        uses_jbig2=bool(opt_info.get("uses_jbig2", False)),
        is_mrc_derivative=bool(opt_info.get("is_mrc_derivative", False)),
        source_page_sha256=sha256_bytes(png_bytes),
        render_px=render_dims,
        native_optical_dpi=opt_info.get("optical_dpi"),
    )


def crop_normalized_bbox(
    source_image_or_pdf: str | Path,
    bbox: list[int] | tuple[int, int, int, int],
    out_path: str | Path,
    *,
    page_number: int | None = None,
    dpi: int = 450,
    pad_norm: int = 10,
) -> Path:
    """Render an isolated `[x0, y0, x1, y1]` (`0..1000`) crop at 450 DPI for hotspot verification."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    x0 = max(0, int(bbox[0]) - pad_norm)
    y0 = max(0, int(bbox[1]) - pad_norm)
    x1 = min(1000, int(bbox[2]) + pad_norm)
    y1 = min(1000, int(bbox[3]) + pad_norm)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"Invalid bbox for crop: {bbox}")

    src = Path(source_image_or_pdf)
    if src.suffix.lower() == ".pdf":
        if page_number is None:
            raise ValueError("page_number is required when cropping directly from a PDF")
        with fitz.open(src) as doc:
            page = doc[page_number - 1]
            rect = page.rect
            clip = fitz.Rect(
                rect.x0 + rect.width * (x0 / 1000.0),
                rect.y0 + rect.height * (y0 / 1000.0),
                rect.x0 + rect.width * (x1 / 1000.0),
                rect.y0 + rect.height * (y1 / 1000.0),
            )
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        with Image.open(src) as raw:
            w, h = raw.size
            img = raw.convert("RGB").crop(
                (int(w * x0 / 1000.0), int(h * y0 / 1000.0), int(w * x1 / 1000.0), int(h * y1 / 1000.0))
            )

    img.save(out, format="PNG")
    return out


def triage_pdf(pdf_path: str | Path, *, sample_pages: list[int] | None = None) -> dict[str, Any]:
    """Run optical DPI, OCR witness, MRC, and JBIG2 lossy-mask triage across a correspondence PDF."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    pdf_sha = sha256_file(path)
    with fitz.open(path) as doc:
        total = int(doc.page_count)
        indices = (
            [p for p in sample_pages if 1 <= p <= total]
            if sample_pages
            else list(range(1, total + 1))
        )
        jbig2_pages: list[int] = []
        mrc_pages: list[int] = []
        ocr_pages = 0
        dpis: list[float] = []
        page_reports: list[dict[str, Any]] = []

        for pnum in indices:
            page = doc[pnum - 1]
            opt = inspect_page_optical_dpi(page)
            wit = (page.get_text("text") or "").strip()
            if len(wit) >= 20:
                ocr_pages += 1
            if opt["uses_jbig2"]:
                jbig2_pages.append(pnum)
            if opt["is_mrc_derivative"]:
                mrc_pages.append(pnum)
            if opt["optical_dpi"] is not None:
                dpis.append(float(opt["optical_dpi"]))
            page_reports.append(
                {
                    "page_number": pnum,
                    "optical_dpi": opt["optical_dpi"],
                    "native_long_px": opt["native_long_px"],
                    "uses_jbig2": opt["uses_jbig2"],
                    "is_mrc_derivative": opt["is_mrc_derivative"],
                    "ocr_chars": len(wit),
                }
            )

    median_dpi = sorted(dpis)[len(dpis) // 2] if dpis else None
    recommended_dpi = 400 if (median_dpi and median_dpi >= 370) else 350
    warnings: list[str] = []
    if jbig2_pages:
        warnings.append(
            f"JBIG2 lossy-mask risk detected on {len(jbig2_pages)} page(s) (first: {jbig2_pages[:8]}); "
            "prefer raw camera JP2 originals via `fetch-archive` to prevent glyph substitution."
        )
    if median_dpi is not None and median_dpi < 200:
        warnings.append(f"Low optical scan DPI ({median_dpi:.1f} DPI); recommend `MEDIA_RESOLUTION_ULTRA_HIGH`.")

    return {
        "pdf_path": str(path),
        "source_pdf_sha256": pdf_sha,
        "total_pages": total,
        "inspected_pages": len(indices),
        "median_optical_dpi": median_dpi,
        "recommended_render_dpi": recommended_dpi,
        "ocr_layer_pages": ocr_pages,
        "jbig2_pages": jbig2_pages,
        "mrc_pages": mrc_pages,
        "warnings": warnings,
        "pages": page_reports,
    }


def pdf_page_count(pdf_path: str | Path) -> int:
    with fitz.open(pdf_path) as doc:
        return int(doc.page_count)


def compute_pdf_sha256(pdf_path: str | Path) -> str:
    return sha256_file(pdf_path)


_ALLOWED_SCAN_EXTS: frozenset[str] = frozenset({".png", ".webp", ".jpg", ".jpeg"})


def collect_existing_local_scans(scans_dir: Path) -> dict[int, str]:
    """Collect image-only `scans/page_NNNN.*` entries while purging stale `.json` artifacts."""
    found: dict[int, str] = {}
    if not scans_dir.is_dir():
        return found
    for stale in scans_dir.glob("*.json"):
        stale.unlink()
    for img_p in sorted(scans_dir.glob("page_*.*")):
        if img_p.suffix.lower() in _ALLOWED_SCAN_EXTS:
            digits = "".join(ch for ch in img_p.stem if ch.isdigit())
            if digits:
                found[int(digits)] = f"scans/{img_p.name}"
    return found


def bundle_local_scans(
    page_numbers: set[int],
    total_pages: int,
    scans_dir: Path,
    *,
    run_dir: Path | None,
    source_pdf: Path | None,
) -> dict[int, str]:
    """Copy existing rendered page scans or render 150-DPI previews from `source_pdf` into `scans_dir`."""
    import shutil

    scans_dir.mkdir(parents=True, exist_ok=True)
    bundled = collect_existing_local_scans(scans_dir)
    if run_dir is not None:
        for cand_sub in (run_dir / "renders", run_dir / "evidence" / "renders"):
            if not cand_sub.is_dir():
                continue
            for img_p in sorted(cand_sub.glob("page_*.*")):
                if img_p.suffix.lower() not in _ALLOWED_SCAN_EXTS:
                    continue
                digits = "".join(ch for ch in img_p.stem if ch.isdigit())
                if not digits:
                    continue
                pnum = int(digits)
                dest = scans_dir / f"page_{pnum:04d}{img_p.suffix.lower()}"
                if not dest.exists():
                    shutil.copy2(img_p, dest)
                bundled[pnum] = f"scans/{dest.name}"
    if not bundled and source_pdf is not None and source_pdf.is_file():
        targets = page_numbers or set(range(1, total_pages + 1))
        with fitz.open(source_pdf) as pdf_doc:
            for pnum in sorted(targets):
                if 1 <= pnum <= len(pdf_doc):
                    pix = pdf_doc[pnum - 1].get_pixmap(dpi=150)
                    dest = scans_dir / f"page_{pnum:04d}.png"
                    pix.save(str(dest))
                    bundled[pnum] = f"scans/{dest.name}"
    return bundled
