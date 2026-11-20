"""Internet Archive (`archive.org`) correspondence discovery, metadata triage, IIIF/BookReader streaming, and JP2/PDF ingest.

ROLE: Discover public-domain correspondence on Archive.org, detect JBIG2 derivative PDF corruption risk, stream scan leaves, and download verified JP2/PDF sources.
INPUTS: Search queries or Archive.org item identifiers.
OUTPUTS: Candidate lists, JBIG2-aware item metadata dicts, canonical scan URLs, and lock-protected SHA-256-verified source files.
INVARIANT: Never silently swallow HTTP/checksum errors; never branch on specific book/author slugs.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pymupdf as fitz
from PIL import Image

from lanterns.core.atomic import atomic_write_json
from lanterns.core.hashes import sha256_file

Image.MAX_IMAGE_PIXELS = 250_000_000
_IMAGE_LEAF_EXTS: tuple[str, ...] = (".jp2", ".j2k", ".jpx", ".tif", ".tiff")

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_BASE = "https://archive.org/metadata"
ARCHIVE_DOWNLOAD_BASE = "https://archive.org/download"
ARCHIVE_IIIF_BASE = "https://iiif.archive.org/iiif"
DEFAULT_USER_AGENT = "PaperLanterns/2.0 (Archival Correspondence Compiler; +https://archive.org)"
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ArchiveSourceError(RuntimeError):
    """Raised when Archive.org discovery, metadata inspection, or verified download fails."""


def _validate_identifier(identifier: str) -> str:
    clean = identifier.strip()
    if not clean or not _SAFE_ID_RE.match(clean) or ".." in clean:
        raise ArchiveSourceError(f"Invalid Archive.org identifier: {identifier!r}")
    return clean


def _build_query_expression(query: str, *, public_domain_only: bool) -> str:
    raw = query.strip()
    if not raw:
        raise ArchiveSourceError("Search query must not be empty")
    has_field_syntax = any(tok in raw for tok in ("title:", "creator:", "subject:", "mediatype:", "collection:"))
    base = raw if has_field_syntax else f"({raw}) AND (correspondence OR letters OR epistles)"
    if "mediatype:" not in base:
        base = f"{base} AND mediatype:texts"
    if public_domain_only and "inlibrary" not in base:
        base = f"{base} AND NOT collection:inlibrary AND NOT access-restricted-item:true"
    return base


def _coerce_creator(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _coerce_year(doc: dict[str, Any]) -> int | None:
    raw = doc.get("year") or doc.get("date") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    match = re.search(r"\b(1[4-9]\d{2}|20\d{2})\b", str(raw))
    return int(match.group(1)) if match else None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_json_with_retry(
    url: str,
    *,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    owns_client = client is None
    active = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_USER_AGENT}) if owns_client else client
    last_err: Exception | None = None
    try:
        for attempt in range(3):
            try:
                resp = active.get(url, params=params)
                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise ArchiveSourceError(f"Archive.org HTTP {resp.status_code} for {url}")
                payload = resp.json()
                if not isinstance(payload, dict) or not payload:
                    raise ArchiveSourceError(f"Empty or non-object JSON payload from {url}")
                return payload
            except ArchiveSourceError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_err = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise ArchiveSourceError(f"Failed requesting {url}: {last_err}")
    finally:
        if owns_client:
            active.close()


def build_iiif_page_url(
    identifier: str,
    leaf_index: int,
    width: int = 1600,
    *,
    use_iiif_endpoint: bool = False,
) -> str:
    """Build canonical Archive.org zero-storage scan page URL.

    Defaults to Archive.org's 0-based accessible BookReader scaler
    (`/download/<id>/page/n<N>_w<W>.jpg`), which aligns 1:1 with accessible
    PDF pages even when `scandata.xml` skips calibration cards (`addToAccessFormats=false`),
    and never triggers IIIF v2 HTTP 403 `>100% scale` rejections on narrow octavos.
    Set `use_iiif_endpoint=True` for the IIIF Image API v2 `full/max` URL.
    """
    clean_id = _validate_identifier(identifier)
    if leaf_index < 0:
        raise ArchiveSourceError(f"leaf_index must be >= 0, got {leaf_index}")
    if width <= 0:
        raise ArchiveSourceError(f"width must be > 0, got {width}")
    quoted_id = quote(clean_id, safe="")
    if use_iiif_endpoint:
        return f"{ARCHIVE_IIIF_BASE}/{quoted_id}${leaf_index}/full/max/0/default.jpg"
    return f"{ARCHIVE_DOWNLOAD_BASE}/{quoted_id}/page/n{leaf_index}_w{width}.jpg"


def build_iiif_info_url(identifier: str, leaf_index: int) -> str:
    """Build IIIF Image API `info.json` tile descriptor URL for OpenSeadragon deep-zoom."""
    clean_id = _validate_identifier(identifier)
    if leaf_index < 0:
        raise ArchiveSourceError(f"leaf_index must be >= 0, got {leaf_index}")
    return f"{ARCHIVE_IIIF_BASE}/{quote(clean_id, safe='')}${leaf_index}/info.json"


def search_archive_correspondence(
    query: str,
    limit: int = 10,
    *,
    public_domain_only: bool = True,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
) -> list[dict[str, Any]]:
    """Search Internet Archive for public-domain correspondence volumes."""
    if limit <= 0:
        raise ArchiveSourceError(f"limit must be > 0, got {limit}")
    q_expr = _build_query_expression(query, public_domain_only=public_domain_only)
    params: list[tuple[str, Any]] = [
        ("q", q_expr),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "creator"),
        ("fl[]", "year"),
        ("fl[]", "date"),
        ("fl[]", "mediatype"),
        ("fl[]", "imagecount"),
        ("fl[]", "language"),
        ("sort[]", "downloads desc"),
        ("rows", min(limit, 100)),
        ("page", 1),
        ("output", "json"),
    ]
    data = _request_json_with_retry(ARCHIVE_SEARCH_URL, params=params, client=client, timeout=timeout)
    docs = data.get("response", {}).get("docs") or []
    results: list[dict[str, Any]] = []
    for doc in docs:
        ident = str(doc.get("identifier") or "").strip()
        if not ident or not _SAFE_ID_RE.match(ident):
            continue
        lang = doc.get("language")
        lang_str = "; ".join(str(x) for x in lang) if isinstance(lang, list) else str(lang or "")
        results.append(
            {
                "identifier": ident,
                "title": str(doc.get("title") or "").strip(),
                "creator": _coerce_creator(doc.get("creator")),
                "year": _coerce_year(doc),
                "mediatype": str(doc.get("mediatype") or "texts"),
                "imagecount": _coerce_int(doc.get("imagecount")),
                "language": lang_str,
                "details_url": f"https://archive.org/details/{ident}",
                "sample_iiif_url": build_iiif_page_url(ident, 1),
            }
        )
        if len(results) >= limit:
            break
    return results


def _normalize_file_entry(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(raw.get("name") or ""),
        "source": str(raw.get("source") or ""),
        "format": str(raw.get("format") or ""),
        "size": _coerce_int(raw.get("size")) or 0,
        "md5": str(raw.get("md5") or ""),
        "sha1": str(raw.get("sha1") or ""),
        "sha256": str(raw.get("sha256") or ""),
    }


def _pick_jp2_zip(clean_id: str, files: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    exact = f"{clean_id}_jp2.zip".lower()
    for entry in files:
        if entry["name"].lower() == exact:
            return entry
    processed = [
        e for e in files if e["name"].lower().endswith("_jp2.zip") and not e["name"].lower().endswith("_raw_jp2.zip")
    ]
    if processed:
        return processed[0]
    jp2_fallback = [e for e in files if e["name"].lower().endswith(".zip") and "jp2" in e["name"].lower()]
    if jp2_fallback:
        return jp2_fallback[0]
    tif_exact = f"{clean_id}_tif.zip".lower()
    for entry in files:
        if entry["name"].lower() == tif_exact:
            return entry
    tif_fallback = [e for e in files if e["name"].lower().endswith("_tif.zip") or (e["name"].lower().endswith(".zip") and "tif" in e["name"].lower())]
    return tif_fallback[0] if tif_fallback else None


def _pick_scandata(clean_id: str, files: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    by_lower = {e["name"].lower(): e for e in files}
    for candidate in (f"{clean_id}_scandata.xml".lower(), "scandata.xml", "scandata.zip"):
        if candidate in by_lower:
            return by_lower[candidate]
    for entry in files:
        low = entry["name"].lower()
        if low.endswith(("_scandata.xml", "scandata.zip")):
            return entry
    return None


def _pick_primary_pdf(clean_id: str, files: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    exact = f"{clean_id}.pdf".lower()
    for entry in files:
        if entry["name"].lower() == exact:
            return entry
    candidates = [
        e
        for e in files
        if e["name"].lower().endswith(".pdf")
        and "/" not in e["name"]
        and not e["name"].lower().endswith(("_bw.pdf", "_text.pdf", ".originals.pdf"))
    ]
    return max(candidates, key=lambda item: item["size"]) if candidates else None


def detect_jbig2_derivative_risk(
    files: Sequence[dict[str, Any]],
    *,
    primary_pdf: dict[str, Any] | None,
    jp2_zip: dict[str, Any] | None,
) -> tuple[bool, str, list[str]]:
    """Detect lossy JBIG2 / MRC 1-bit mask glyph-substitution risk in Archive.org derivative PDFs."""
    reasons: list[str] = []
    bw_pdfs = [
        e["name"]
        for e in files
        if e["name"].lower().endswith("_bw.pdf") or "grayscale pdf" in e["format"].lower()
    ]
    if bw_pdfs:
        reasons.append(f"Bitonal JBIG2 derivative PDF present ({bw_pdfs[0]}) — never use `_bw.pdf` for transcription.")

    explicit_jbig2 = [e["name"] for e in files if "jbig2" in e["name"].lower() or "jbig2" in e["format"].lower()]
    if explicit_jbig2:
        reasons.append(f"Explicit JBIG2 artifact listed ({explicit_jbig2[0]}).")

    if primary_pdf is not None:
        fmt_low = primary_pdf["format"].lower()
        src_low = primary_pdf["source"].lower()
        is_derivative_pdf = src_low == "derivative" or fmt_low in {"text pdf", "additional text pdf", "image container pdf"}
        if is_derivative_pdf:
            reasons.append(
                f"Primary PDF ({primary_pdf['name']}, format={primary_pdf['format']!r}) is an Archive.org derivative "
                "typically encoded with a 1-bit JBIG2 /SMask layer prone to glyph/digit substitution and MRC grey blots."
            )
        if jp2_zip is not None and primary_pdf["size"] > 0 and jp2_zip["size"] >= primary_pdf["size"] * 2.5:
            ratio = round(jp2_zip["size"] / primary_pdf["size"], 1)
            reasons.append(
                f"Unblotted JP2 camera archive ({jp2_zip['name']}) is {ratio}x larger than {primary_pdf['name']}, "
                "confirming heavy MRC/JBIG2 derivative compression."
            )

    if not reasons:
        return False, "low", []
    high = primary_pdf is not None and (primary_pdf["source"].lower() == "derivative" or bool(explicit_jbig2))
    return True, ("high" if high else "moderate"), reasons


def fetch_archive_metadata(
    identifier: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch and analyze Archive.org item metadata (`_jp2.zip`, `_scandata.xml`, `.pdf`, JBIG2 risk)."""
    clean_id = _validate_identifier(identifier)
    raw = _request_json_with_retry(f"{ARCHIVE_METADATA_BASE}/{quote(clean_id, safe='')}", client=client, timeout=timeout)
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    raw_files = raw.get("files") if isinstance(raw.get("files"), list) else []
    files = [_normalize_file_entry(f) for f in raw_files if isinstance(f, dict) and f.get("name")]
    if not files:
        raise ArchiveSourceError(f"Archive.org item {clean_id!r} exposes zero downloadable files")

    jp2_entry = _pick_jp2_zip(clean_id, files)
    scandata_entry = _pick_scandata(clean_id, files)
    pdf_entry = _pick_primary_pdf(clean_id, files)
    bw_entry = next((e for e in files if e["name"].lower().endswith("_bw.pdf")), None)
    jbig2_risk, risk_level, risk_reasons = detect_jbig2_derivative_risk(
        files, primary_pdf=pdf_entry, jp2_zip=jp2_entry
    )
    if jp2_entry is not None and (jbig2_risk or pdf_entry is None):
        recommended = "jp2_zip"
    elif pdf_entry is not None:
        recommended = "pdf"
    else:
        recommended = "iiif_stream"

    return {
        "identifier": clean_id,
        "title": str(meta.get("title") or "").strip(),
        "creator": _coerce_creator(meta.get("creator")),
        "year": _coerce_year(meta),
        "mediatype": str(meta.get("mediatype") or "texts"),
        "imagecount": _coerce_int(meta.get("imagecount")),
        "ppi": _coerce_int(meta.get("ppi")),
        "server": str(raw.get("server") or ""),
        "dir": str(raw.get("dir") or ""),
        "has_jp2_zip": jp2_entry is not None,
        "has_scandata": scandata_entry is not None,
        "has_pdf": pdf_entry is not None,
        "jp2_zip": jp2_entry,
        "scandata": scandata_entry,
        "pdf": pdf_entry,
        "bw_pdf": bw_entry,
        "jbig2_risk": jbig2_risk,
        "jbig2_risk_level": risk_level,
        "jbig2_risk_reasons": risk_reasons,
        "recommended_source": recommended,
        "sample_iiif_url": build_iiif_page_url(clean_id, 0),
        "files": files,
    }


def _leaf_number(name: str) -> int:
    match = re.search(r"_(\d{4,5})\.(?:jp2|j2k|jpx|tif|tiff|jpg|jpeg)$", name, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def parse_scandata_leaves(scandata_xml: str) -> list[tuple[int, bool]]:
    """Parse `(leaf_num, included_in_access)` from Scribe `scandata.xml`."""
    if not scandata_xml.strip():
        return []
    attr_matches = re.findall(r'<page\s+[^>]*leafNum="(\d+)"[^>]*>(.*?)</page>', scandata_xml, re.S)
    if attr_matches:
        return [
            (int(num), not bool(re.search(r"<addToAccessFormats>\s*false\s*</addToAccessFormats>", body, re.I)))
            for num, body in attr_matches
        ]
    entries: list[tuple[int, bool]] = []
    for body in re.findall(r"<page>(.*?)</page>", scandata_xml, re.S):
        m_num = re.search(r"<leafNum>\s*(\d+)\s*</leafNum>", body)
        if m_num:
            included = not bool(re.search(r"<addToAccessFormats>\s*false\s*</addToAccessFormats>", body, re.I))
            entries.append((int(m_num.group(1)), included))
    return entries


def _leaves_excluding(numbered: Sequence[tuple[int, str]], excluded: set[int]) -> list[str]:
    return [name for num, name in numbered if num not in excluded]


def access_leaves(
    jp2_names: Sequence[str],
    scandata_xml: str,
    *,
    expected_page_count: int | None = None,
) -> list[str]:
    """Return JP2/TIFF leaf paths included in the access edition in deterministic page order."""
    sorted_names = sorted(jp2_names)
    parsed = parse_scandata_leaves(scandata_xml)
    if not parsed:
        return sorted_names
    numbered = [(_leaf_number(n), n) for n in sorted_names]
    leaf_nums = [num for num, _ in numbered if num >= 0]
    xml_nums = [num for num, _ in parsed]
    excluded = {num for num, inc in parsed if not inc}

    offset = (min(leaf_nums) - min(xml_nums)) if (leaf_nums and xml_nums) else 0
    aligned_preferred = bool(
        offset != 0 and len(xml_nums) == len(leaf_nums) and max(leaf_nums) == max(xml_nums) + offset
    )
    direct = _leaves_excluding(numbered, excluded)
    aligned = _leaves_excluding(numbered, {n + offset for n in excluded}) if offset != 0 else direct

    if expected_page_count is None:
        return aligned if aligned_preferred else direct

    candidates = [aligned, direct] if aligned_preferred else [direct, aligned]
    for cand in candidates:
        if len(cand) == expected_page_count:
            return cand
    for shift in (1, -1):
        shifted = _leaves_excluding(numbered, {n + shift for n in excluded})
        if len(shifted) == expected_page_count:
            return shifted
    return candidates[0]


def _verify_file_hashes(
    path: Path,
    *,
    expected_size: int = 0,
    expected_sha256: str = "",
    expected_sha1: str = "",
    expected_md5: str = "",
) -> str:
    actual_size = path.stat().st_size
    if expected_size > 0 and actual_size != expected_size:
        raise ArchiveSourceError(
            f"Size mismatch for {path.name}: got {actual_size} bytes, expected {expected_size}"
        )
    h_sha256 = hashlib.sha256()
    h_sha1 = hashlib.sha1() if expected_sha1 else None
    h_md5 = hashlib.md5() if expected_md5 else None
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h_sha256.update(chunk)
            if h_sha1 is not None:
                h_sha1.update(chunk)
            if h_md5 is not None:
                h_md5.update(chunk)
    digest_256 = h_sha256.hexdigest()
    if expected_sha256 and digest_256.lower() != expected_sha256.lower():
        raise ArchiveSourceError(
            f"SHA-256 mismatch for {path.name}: got {digest_256}, expected {expected_sha256}"
        )
    if h_sha1 is not None and h_sha1.hexdigest().lower() != expected_sha1.lower():
        raise ArchiveSourceError(f"SHA-1 mismatch for {path.name}: expected {expected_sha1}")
    if h_md5 is not None and h_md5.hexdigest().lower() != expected_md5.lower():
        raise ArchiveSourceError(f"MD5 mismatch for {path.name}: expected {expected_md5}")
    return digest_256


def _cached_file_valid(dst: Path, sidecar: Path, *, expected_size: int, expected_sha256: str) -> str | None:
    if not dst.is_file():
        return None
    if expected_size > 0 and dst.stat().st_size != expected_size:
        return None
    digest = sha256_file(dst)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        return None
    if sidecar.is_file():
        try:
            import json

            recorded = json.loads(sidecar.read_text(encoding="utf-8")).get("sha256", "")
            if recorded and recorded.lower() != digest.lower():
                return None
        except (OSError, ValueError):
            return None
    return digest


def _stream_url_locked(
    url: str,
    dst: Path,
    *,
    expected_size: int = 0,
    expected_sha256: str = "",
    expected_sha1: str = "",
    expected_md5: str = "",
    client: httpx.Client,
) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sidecar = dst.with_suffix(dst.suffix + ".sha256.json")
    lock_path = dst.with_suffix(dst.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            cached = _cached_file_valid(dst, sidecar, expected_size=expected_size, expected_sha256=expected_sha256)
            if cached is not None:
                return {"path": str(dst), "sha256": cached, "size_bytes": dst.stat().st_size, "reused_cache": True}

            part = dst.with_suffix(f"{dst.suffix}.part.{os.getpid()}")
            last_err: Exception | None = None
            try:
                for attempt in range(3):
                    try:
                        with client.stream("GET", url) as resp:
                            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < 2:
                                time.sleep(1.5 * (attempt + 1))
                                continue
                            if resp.status_code != 200:
                                raise ArchiveSourceError(f"Archive.org download HTTP {resp.status_code} for {url}")
                            with part.open("wb") as out_fh:
                                for chunk in resp.iter_bytes(chunk_size=1 << 18):
                                    if chunk:
                                        out_fh.write(chunk)
                                out_fh.flush()
                                os.fsync(out_fh.fileno())
                        digest = _verify_file_hashes(
                            part,
                            expected_size=expected_size,
                            expected_sha256=expected_sha256,
                            expected_sha1=expected_sha1,
                            expected_md5=expected_md5,
                        )
                        part.replace(dst)
                        record = {
                            "url": url,
                            "file": dst.name,
                            "sha256": digest,
                            "size_bytes": dst.stat().st_size,
                        }
                        atomic_write_json(sidecar, record)
                        return {"path": str(dst), "sha256": digest, "size_bytes": record["size_bytes"], "reused_cache": False}
                    except ArchiveSourceError as exc:
                        last_err = exc
                        if attempt < 2 and "HTTP 5" in str(exc):
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        raise
                    except (httpx.HTTPError, OSError) as exc:
                        last_err = exc
                        if attempt < 2:
                            time.sleep(1.5 * (attempt + 1))
                raise ArchiveSourceError(f"Failed downloading {url}: {last_err}")
            finally:
                part.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _download_leaf_slice(
    clean_id: str,
    dest_root: Path,
    leaf_indices: Sequence[int],
    *,
    width: int,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    leaves_dir = dest_root / "leaves"
    downloaded: list[dict[str, Any]] = []
    for idx in leaf_indices:
        if idx < 0:
            raise ArchiveSourceError(f"Invalid negative leaf index {idx}")
        url = build_iiif_page_url(clean_id, idx, width=width)
        target = leaves_dir / f"leaf_{idx:04d}.jpg"
        info = _stream_url_locked(url, target, client=client)
        info["leaf_index"] = idx
        downloaded.append(info)
    return downloaded


def _extract_jp2_slice_if_requested(jp2_zip_path: Path, dest_root: Path, leaf_indices: Sequence[int] | None) -> list[str]:
    if not leaf_indices:
        return []
    out_dir = dest_root / "jp2_leaves"
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    wanted = set(leaf_indices)
    with zipfile.ZipFile(jp2_zip_path) as zf:
        jp2_members = sorted(n for n in zf.namelist() if n.lower().endswith(_IMAGE_LEAF_EXTS))
        for pos, member in enumerate(jp2_members):
            leaf_no = _leaf_number(member)
            if pos in wanted or leaf_no in wanted:
                dst = out_dir / Path(member).name
                part = dst.with_suffix(f".part.{os.getpid()}")
                part.write_bytes(zf.read(member))
                part.replace(dst)
                extracted.append(str(dst))
    return extracted


def _select_download_entry(meta: dict[str, Any], *, prefer_jp2: bool) -> dict[str, Any]:
    primary = meta["jp2_zip"] if prefer_jp2 else meta["pdf"]
    fallback = meta["pdf"] if prefer_jp2 else meta["jp2_zip"]
    selected = primary or fallback
    if selected is None:
        raise ArchiveSourceError(
            f"No downloadable `_jp2.zip` or `.pdf` found in Archive.org item {meta['identifier']!r}"
        )
    return selected


def _download_metadata_entry(
    clean_id: str,
    entry: dict[str, Any],
    dest_root: Path,
    *,
    client: httpx.Client,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    rel_name = entry["name"].lstrip("/")
    file_url = f"{ARCHIVE_DOWNLOAD_BASE}/{quote(clean_id, safe='')}/{quote(rel_name, safe='/')}"
    return _stream_url_locked(
        file_url,
        dest_root / Path(rel_name).name,
        expected_size=int(entry.get("size") or 0),
        expected_sha256=expected_sha256 or str(entry.get("sha256") or ""),
        expected_sha1=str(entry.get("sha1") or ""),
        expected_md5=str(entry.get("md5") or ""),
        client=client,
    )


def _read_scandata_text(scandata_path: Path | None) -> str:
    if scandata_path is None or not scandata_path.is_file():
        return ""
    if scandata_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(scandata_path) as zf:
            xml_members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not xml_members:
                return ""
            chosen_xml = next(
                (n for n in xml_members if Path(n).name.lower().endswith("scandata.xml")),
                xml_members[0],
            )
            raw = zf.read(chosen_xml)
    else:
        raw = scandata_path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _parse_scandata_dpi(scandata_xml: str, fallback_ppi: int | None = None) -> int:
    if fallback_ppi is not None and fallback_ppi >= 72:
        return fallback_ppi
    match = re.search(r"<(?:dpi|ppi)>\s*(\d+)\s*</(?:dpi|ppi)>", scandata_xml, re.IGNORECASE)
    if match:
        parsed = int(match.group(1))
        if parsed >= 72:
            return parsed
    return 300


def _cached_originals_pdf(
    originals_pdf: Path,
    sidecar: Path,
    *,
    source_zip_sha256: str,
) -> dict[str, Any] | None:
    if not originals_pdf.is_file() or not sidecar.is_file():
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if source_zip_sha256 and meta.get("source_zip_sha256") != source_zip_sha256:
        return None
    size_bytes = originals_pdf.stat().st_size
    if int(meta.get("size_bytes") or 0) != size_bytes:
        return None
    digest = sha256_file(originals_pdf)
    if str(meta.get("sha256") or "").lower() != digest.lower():
        return None
    return {
        "path": str(originals_pdf),
        "sha256": digest,
        "size_bytes": size_bytes,
        "page_count": int(meta.get("page_count") or 0),
        "optical_dpi": int(meta.get("optical_dpi") or 300),
        "reused_cache": True,
    }


def _prepare_embed_stream(member: str, raw_bytes: bytes) -> tuple[bytes, int, int]:
    with Image.open(io.BytesIO(raw_bytes)) as img:
        width_px, height_px = img.size
        if member.lower().endswith((".tif", ".tiff")):
            buf = io.BytesIO()
            out_img = img if img.mode in {"1", "L", "RGB"} else img.convert("RGB")
            out_img.save(buf, format="PNG")
            return buf.getvalue(), width_px, height_px
        return raw_bytes, width_px, height_px


def _embed_jp2_members_into_pdf(
    zf: zipfile.ZipFile,
    members: Sequence[str],
    out_part: Path,
    *,
    dpi: int,
    derivative_pdf_path: Path | None = None,
) -> int:
    doc = fitz.open()
    deriv_doc = (
        fitz.open(derivative_pdf_path)
        if derivative_pdf_path is not None and derivative_pdf_path.is_file()
        else None
    )
    use_deriv = deriv_doc is not None and deriv_doc.page_count == len(members)
    try:
        for idx, member in enumerate(members):
            stream_bytes, width_px, height_px = _prepare_embed_stream(member, zf.read(member))
            if use_deriv and deriv_doc is not None:
                doc.insert_pdf(deriv_doc, from_page=idx, to_page=idx)
                target = doc[-1]
                rot = int(deriv_doc[idx].rotation or 0)
                target.insert_image(target.rect, stream=stream_bytes, keep_proportion=False, overlay=True, rotate=rot)
            else:
                page = doc.new_page(width=width_px * 72.0 / dpi, height=height_px * 72.0 / dpi)
                page.insert_image(page.rect, stream=stream_bytes)
        doc.save(out_part)
        return len(members)
    finally:
        if deriv_doc is not None:
            deriv_doc.close()
        doc.close()


def build_originals_pdf_from_jp2_zip(
    jp2_zip_path: str | Path,
    originals_pdf_path: str | Path,
    *,
    scandata_xml: str = "",
    ppi: int | None = None,
    expected_page_count: int | None = None,
    source_zip_sha256: str = "",
    derivative_pdf_path: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble a lossless JBIG2-free `.originals.pdf` from raw `.jp2`/`.tif` camera streams."""
    zip_path = Path(jp2_zip_path)
    out_path = Path(originals_pdf_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256.json")
    lock_path = out_path.with_suffix(out_path.suffix + ".lock")
    deriv_path = Path(derivative_pdf_path) if derivative_pdf_path is not None else None
    resolved_expected = expected_page_count
    if resolved_expected is None and deriv_path is not None and deriv_path.is_file():
        with fitz.open(deriv_path) as dpdf:
            resolved_expected = dpdf.page_count

    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            cached = _cached_originals_pdf(out_path, sidecar, source_zip_sha256=source_zip_sha256)
            if cached is not None:
                return cached
            dpi = _parse_scandata_dpi(scandata_xml, fallback_ppi=ppi)
            part = out_path.with_suffix(f"{out_path.suffix}.part.{os.getpid()}")
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    jp2_names = [n for n in zf.namelist() if n.lower().endswith(_IMAGE_LEAF_EXTS)]
                    if not jp2_names:
                        raise ArchiveSourceError(f"No `.jp2`/`.tif` leaves found inside {zip_path.name}")
                    selected = access_leaves(jp2_names, scandata_xml, expected_page_count=resolved_expected)
                    page_count = _embed_jp2_members_into_pdf(
                        zf,
                        selected,
                        part,
                        dpi=dpi,
                        derivative_pdf_path=deriv_path,
                    )
                digest = sha256_file(part)
                part.replace(out_path)
                record = {
                    "file": out_path.name,
                    "sha256": digest,
                    "size_bytes": out_path.stat().st_size,
                    "page_count": page_count,
                    "optical_dpi": dpi,
                    "source_zip": zip_path.name,
                    "source_zip_sha256": source_zip_sha256,
                    "preserved_ocr_witness": bool(deriv_path and deriv_path.is_file()),
                }
                atomic_write_json(sidecar, record)
                return {
                    "path": str(out_path),
                    "sha256": digest,
                    "size_bytes": record["size_bytes"],
                    "page_count": page_count,
                    "optical_dpi": dpi,
                    "preserved_ocr_witness": record["preserved_ocr_witness"],
                    "reused_cache": False,
                }
            finally:
                part.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def download_archive_source(
    identifier: str,
    dest_dir: str | Path,
    prefer_jp2: bool = True,
    *,
    leaf_indices: Sequence[int] | None = None,
    stream_slice_only: bool = False,
    slice_width: int = 2200,
    expected_sha256: str | None = None,
    include_scandata: bool = True,
    build_originals_pdf: bool = True,
    expected_page_count: int | None = None,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Download verified PDF / JP2 archive (or zero-bloat leaf slice) with `.lock` safety and SHA-256 verification."""
    clean_id = _validate_identifier(identifier)
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    active = (
        httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_USER_AGENT})
        if owns_client
        else client
    )
    try:
        meta = fetch_archive_metadata(clean_id, client=active, timeout=min(timeout, 30.0))
        if stream_slice_only and leaf_indices:
            leaves = _download_leaf_slice(clean_id, dest_root, leaf_indices, width=slice_width, client=active)
            manifest = {
                "identifier": clean_id,
                "mode": "iiif_leaf_slice",
                "jbig2_risk": meta["jbig2_risk"],
                "leaves": leaves,
            }
            atomic_write_json(dest_root / f"{clean_id}.source_manifest.json", manifest)
            return manifest

        selected = _select_download_entry(meta, prefer_jp2=prefer_jp2)
        primary_dl = _download_metadata_entry(
            clean_id, selected, dest_root, client=active, expected_sha256=expected_sha256
        )
        scandata_dl = (
            _download_metadata_entry(clean_id, meta["scandata"], dest_root, client=active)
            if (include_scandata and meta["scandata"] is not None)
            else None
        )
        dst_file = Path(primary_dl["path"])
        is_zip = dst_file.suffix.lower() == ".zip"
        extracted_leaves = _extract_jp2_slice_if_requested(dst_file, dest_root, leaf_indices) if is_zip else []
        originals_info: dict[str, Any] | None = None
        deriv_warning: str | None = None
        if is_zip and build_originals_pdf and not leaf_indices:
            scandata_xml = _read_scandata_text(Path(scandata_dl["path"]) if scandata_dl else None)
            local_deriv: Path | None = None
            if isinstance(meta.get("pdf"), dict) and meta["pdf"].get("name"):
                cand_deriv = dest_root / Path(str(meta["pdf"]["name"])).name
                if not cand_deriv.is_file():
                    try:
                        deriv_dl = _download_metadata_entry(clean_id, meta["pdf"], dest_root, client=active)
                        cand_deriv = Path(deriv_dl["path"])
                    except Exception as exc:
                        deriv_warning = str(exc)
                if cand_deriv.is_file():
                    local_deriv = cand_deriv
            originals_info = build_originals_pdf_from_jp2_zip(
                dst_file,
                dest_root / f"{clean_id}.originals.pdf",
                scandata_xml=scandata_xml,
                ppi=meta.get("ppi"),
                expected_page_count=expected_page_count,
                source_zip_sha256=str(primary_dl.get("sha256") or ""),
                derivative_pdf_path=local_deriv,
            )
            if deriv_warning:
                originals_info["derivative_pdf_warning"] = deriv_warning
        result = {
            "identifier": clean_id,
            "mode": "jp2_zip" if is_zip else "pdf",
            "selected_file": selected["name"],
            "primary": primary_dl,
            "scandata": scandata_dl,
            "originals_pdf": originals_info,
            "extracted_leaves": extracted_leaves,
            "jbig2_risk": meta["jbig2_risk"],
            "jbig2_risk_level": meta["jbig2_risk_level"],
            "jbig2_risk_reasons": meta["jbig2_risk_reasons"],
        }
        atomic_write_json(dest_root / f"{clean_id}.source_manifest.json", result)
        return result
    finally:
        if owns_client:
            active.close()
