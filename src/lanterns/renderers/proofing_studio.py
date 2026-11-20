"""Distributed Proofreaders (P1 -> P2) Static Community Proofing Studio (`proofing/`).

ROLE: Emit a safe-DOM, strict-CSP static proofreading bundle with IIIF/offline scan zoom, bbox sync, and P1->P2 sign-off.
INPUTS: CollectionAST, optional proof_ledger dict, optional run_dir/source_pdf for local scan bundling.
OUTPUTS: proofing/{index.html, styles.css, app.js, manifest.json, pages/pNNNN.json, scans/*}.
INVARIANT: Safe DOM (createElement/textContent only); strict CSP; P2 verifier != P1 reviewer.
"""

from __future__ import annotations

import hashlib
import html
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lanterns.core.models import BlockIR, CollectionAST, PageIR
from lanterns.renderers.html_utils import atomic_write_json, atomic_write_text
from lanterns.source.archive_org import build_iiif_page_url

DIACRITIC_PALETTE: tuple[str, ...] = (
    "ā", "ī", "ū", "ṛ", "ṝ", "ḷ", "ṭ", "ḍ", "ṇ", "ś", "ṣ", "ṃ", "ḥ", "ṅ", "ñ",
    "Ā", "Ī", "Ū", "Ṛ", "Ṭ", "Ḍ", "Ṇ", "Ś", "Ṣ", "Ṅ", "Ñ",
    "á", "é", "í", "ó", "ú", "à", "è", "ù", "â", "ê", "î", "ô", "û", "ä", "ö", "ü", "ß", "ç",
    "æ", "Æ", "œ", "Œ", "ſ", "ḥ", "ẓ", "ġ", "ʿ", "ʾ", "—", "–", "†", "‡", "§", "‖", "¶",
)

STRICT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: https://iiif.archive.org https://archive.org; "
    "connect-src 'self' https://iiif.archive.org; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _build_proofing_csp(relay_url: str = "") -> str:
    parts = urlsplit(relay_url.strip()) if relay_url else None
    origin = (
        f" {parts.scheme}://{parts.netloc}"
        if (parts and parts.scheme in {"http", "https"} and parts.netloc)
        else ""
    )
    return STRICT_CSP.replace(
        "connect-src 'self' https://iiif.archive.org;",
        f"connect-src 'self' https://iiif.archive.org{origin};",
    )


def compute_page_blocks_sha256(blocks: list[BlockIR]) -> str:
    """Compute canonical SHA-256 matching `epistolary.corrections.compute_page_text_sha256`."""
    lines = [
        f"{b.type}:{b.alignment}:{b.epistolary_transition}:{b.tagged_text}"
        for b in blocks
        if b.type not in {"scanner_residue", "noise"}
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


_ALLOWED_SCAN_EXTS: frozenset[str] = frozenset({".png", ".webp", ".jpg", ".jpeg"})


def _collect_existing_local_scans(scans_dir: Path) -> dict[int, str]:
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


def _bundle_local_scans(
    ast: CollectionAST,
    scans_dir: Path,
    *,
    run_dir: Path | None,
    source_pdf: Path | None,
) -> dict[int, str]:
    scans_dir.mkdir(parents=True, exist_ok=True)
    bundled = _collect_existing_local_scans(scans_dir)
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
        import pymupdf

        page_nums = {p.page_number for p in ast.pages} or set(range(1, ast.total_pages + 1))
        with pymupdf.open(source_pdf) as pdf_doc:
            for pnum in sorted(page_nums):
                if 1 <= pnum <= len(pdf_doc):
                    pix = pdf_doc[pnum - 1].get_pixmap(dpi=150)
                    dest = scans_dir / f"page_{pnum:04d}.png"
                    pix.save(str(dest))
                    bundled[pnum] = f"scans/{dest.name}"
    return bundled


def _build_page_shard(
    page: PageIR,
    *,
    page_letters: list[dict[str, Any]],
    page_findings: list[dict[str, Any]],
    ledger_entry: dict[str, Any],
    local_scan: str | None,
    archive_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pnum = page.page_number
    iiif_url = build_iiif_page_url(archive_id, max(0, pnum - 1), width=1600) if archive_id else None
    primary_scan = local_scan or iiif_url or f"scans/page_{pnum:04d}.png"
    fallback_scan = iiif_url if (local_scan and iiif_url) else None

    status = str(ledger_entry.get("status") or "unproofed")
    is_human = str(ledger_entry.get("signoff_method") or "human") == "human" and status in {"p1_proofed", "verified"}
    p1_rev = (
        (ledger_entry.get("p1_reviewer_id") or (ledger_entry.get("reviewer") if status == "p1_proofed" else None))
        if is_human
        else None
    )
    p2_ver = ledger_entry.get("p2_verifier_id") if (is_human and status == "verified") else None
    text_sha = compute_page_blocks_sha256(page.blocks)

    blocks_json = [
        {
            "block_id": blk.block_id,
            "reading_order": blk.reading_order,
            "type": blk.type,
            "alignment": blk.alignment,
            "epistolary_transition": blk.epistolary_transition,
            "line_breaks_preserved": blk.line_breaks_preserved,
            "note_label": blk.note_label,
            "bbox": blk.bbox,
            "tagged_text": blk.tagged_text,
            "plain_text": blk.plain_text,
        }
        for blk in page.blocks
    ]

    shard = {
        "page_number": pnum,
        "printed_folio": page.printed_folio or str(pnum),
        "version": int(ledger_entry.get("version") or 0),
        "failed_closed": page.failed_closed,
        "failure_reason": page.failure_reason,
        "source_page_sha256": page.source_page_sha256,
        "page_text_sha256": text_sha,
        "proof_status": status,
        "p1_reviewer": p1_rev,
        "p2_verifier": p2_ver,
        "scan_url": primary_scan,
        "fallback_scan_url": fallback_scan,
        "findings": page_findings,
        "letters_on_page": page_letters,
        "blocks": blocks_json,
    }
    summary = {
        "page_number": pnum,
        "folio": page.printed_folio or str(pnum),
        "proof_status": status,
        "p1_reviewer": p1_rev,
        "p2_verifier": p2_ver,
        "hotspot_count": len(page_findings),
        "scan_url": primary_scan,
        "fallback_scan_url": fallback_scan,
        "source_page_sha256": page.source_page_sha256,
    }
    return shard, summary


PROOFING_CSS = """/* Paper Lanterns v2 — Safe-DOM Distributed Proofreaders Studio */
:root {
  --paper: #faf7f2;
  --panel: #f2ece2;
  --card: #fffdf9;
  --ink: #1a1816;
  --muted: #6f675e;
  --accent: #6e242b;
  --verified: #1f5c3a;
  --p1: #8a5a14;
  --border: rgba(26, 24, 22, 0.18);
  --serif: "EB Garamond", Georgia, serif;
  --sans: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}

* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--sans); font-size: 14px; }

.studio-topbar {
  display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
  gap: 0.6rem 1rem; padding: 0.65rem 1.2rem; background: var(--panel); border-bottom: 1px solid var(--border);
}
.studio-brand { font-family: var(--serif); font-size: 1.15rem; font-weight: 600; }
.studio-controls { display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center; }

button, select, input {
  font-family: var(--sans); font-size: 0.82rem; padding: 0.35rem 0.6rem;
  border: 1px solid var(--border); background: var(--card); color: var(--ink);
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); color: var(--accent); }
button.primary-btn { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }

.studio-grid {
  display: grid; grid-template-columns: 1fr 1fr; height: calc(100vh - 3.3rem);
}
@media (max-width: 900px) {
  .studio-grid { grid-template-columns: 1fr; height: auto; }
}

.scan-pane {
  display: flex; flex-direction: column; border-right: 1px solid var(--border); background: #262320; color: #f5f0e8; overflow: hidden;
}
.scan-toolbar {
  display: flex; justify-content: space-between; align-items: center; padding: 0.45rem 0.8rem;
  background: #1d1b18; border-bottom: 1px solid rgba(255,255,255,0.12); font-size: 0.78rem;
}
.scan-scroll { flex: 1; overflow: auto; padding: 0.75rem; position: relative; }
.scan-stage { position: relative; display: inline-block; transform-origin: top left; }
.scan-stage.zoom-100 { width: 100%; }
.scan-stage.zoom-150 { width: 150%; }
.scan-stage.zoom-200 { width: 200%; }
.scan-stage.zoom-275 { width: 275%; }
.scan-stage img { display: block; width: 100%; height: auto; }
.bbox-overlay {
  position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none;
}
.bbox-rect {
  fill: rgba(110, 36, 43, 0.16); stroke: #d93848; stroke-width: 4; vector-effect: non-scaling-stroke;
}
.bbox-hidden { display: none; }

.editor-pane {
  display: flex; flex-direction: column; overflow: hidden; background: var(--paper);
}
.palette-bar {
  display: flex; flex-wrap: wrap; gap: 0.25rem; padding: 0.45rem 0.8rem;
  background: var(--panel); border-bottom: 1px solid var(--border);
}
.palette-bar button { padding: 0.2rem 0.42rem; font-family: var(--serif); font-size: 0.92rem; }

.editor-scroll { flex: 1; overflow-y: auto; padding: 0.9rem 1.1rem; }

.status-banner {
  padding: 0.55rem 0.8rem; margin-bottom: 0.8rem; border-left: 3px solid var(--accent);
  background: var(--panel); font-size: 0.82rem;
}
.status-verified { border-left-color: var(--verified); }
.status-p1 { border-left-color: var(--p1); }

.block-card {
  border: 1px solid var(--border); background: var(--card); padding: 0.65rem 0.75rem; margin-bottom: 0.7rem;
}
.block-card.active-card { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.block-card-head {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.4rem; margin-bottom: 0.45rem; font-size: 0.76rem; color: var(--muted);
}
.note-label-input { width: 5.5rem; }
.block-textarea {
  width: 100%; min-height: 4.2rem; font-family: var(--serif); font-size: 1.05rem; line-height: 1.5;
  padding: 0.45rem; border: 1px solid var(--border); background: #fff; color: var(--ink); resize: vertical;
}

.epistolary-panel, .signoff-panel {
  border-top: 1px solid var(--border); padding: 0.75rem 1.1rem; background: var(--panel);
}
.panel-title { font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 0.5rem; }
.signoff-row { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.feedback-msg { margin-top: 0.45rem; font-size: 0.8rem; color: var(--accent); font-weight: 600; }
"""


PROOFING_JS = """/* Paper Lanterns v2 — Safe-DOM Distributed Proofreaders (P1 -> P2) Studio */
(function () {
  "use strict";

  var state = {
    manifest: null,
    currentPage: 1,
    shard: null,
    activeBlockIndex: -1,
    activeTextarea: null,
    editsByBlock: {},
    forceSplits: {},
    forbidSplits: {},
    leaseToken: null,
    baseVersion: 0,
    zoomIndex: 0
  };
  var ZOOM_CLASSES = ["zoom-100", "zoom-150", "zoom-200", "zoom-275"];

  function clearChildren(node) {
    while (node && node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function makeEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function pad4(n) {
    return String(n).padStart(4, "0");
  }

  async function sha256Hex(str) {
    if (window.crypto && window.crypto.subtle) {
      var bytes = new TextEncoder().encode(str);
      var buf = await window.crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(buf)).map(function (b) { return b.toString(16).padStart(2, "0"); }).join("");
    }
    return "";
  }

  function insertAtCursor(textarea, snippet, wrapClose) {
    if (!textarea) return;
    var start = textarea.selectionStart || 0;
    var end = textarea.selectionEnd || 0;
    var val = textarea.value;
    var selected = val.slice(start, end);
    var replacement = wrapClose ? (snippet + selected + wrapClose) : snippet;
    textarea.value = val.slice(0, start) + replacement + val.slice(end);
    textarea.focus();
    var cursor = start + replacement.length;
    textarea.setSelectionRange(cursor, cursor);
    textarea.dispatchEvent(new Event("input"));
  }

  function updateBboxHighlight(bbox) {
    var rect = document.getElementById("active-bbox-rect");
    if (!rect) return;
    if (!bbox || bbox.length !== 4) {
      rect.setAttribute("class", "bbox-rect bbox-hidden");
      return;
    }
    var x = Math.max(0, Number(bbox[0]) || 0);
    var y = Math.max(0, Number(bbox[1]) || 0);
    var w = Math.max(10, (Number(bbox[2]) || 1000) - x);
    var h = Math.max(10, (Number(bbox[3]) || 1000) - y);
    rect.setAttribute("x", String(x));
    rect.setAttribute("y", String(y));
    rect.setAttribute("width", String(w));
    rect.setAttribute("height", String(h));
    rect.setAttribute("class", "bbox-rect");
  }

  function setZoom(idx) {
    state.zoomIndex = Math.max(0, Math.min(ZOOM_CLASSES.length - 1, idx));
    var stage = document.getElementById("scan-stage");
    var label = document.getElementById("zoom-label");
    if (stage) {
      stage.className = "scan-stage " + ZOOM_CLASSES[state.zoomIndex];
    }
    if (label) {
      var pct = ZOOM_CLASSES[state.zoomIndex].replace("zoom-", "") + "%";
      label.textContent = pct;
    }
  }

  function renderScanView(shard) {
    var img = document.getElementById("scan-image");
    var caption = document.getElementById("scan-caption");
    if (!img || !caption) return;
    caption.textContent = "Leaf " + shard.page_number + " (p. " + shard.printed_folio + ")";
    img.onerror = function () {
      if (shard.fallback_scan_url && img.src !== shard.fallback_scan_url) {
        img.src = shard.fallback_scan_url;
      }
    };
    img.src = shard.scan_url;
    if (window.OpenSeadragon && typeof window.OpenSeadragon === "function" && state.manifest.use_osd) {
      window.OpenSeadragon({ id: "osd-hook", tileSources: { type: "image", url: shard.scan_url } });
    }
  }

  function renderHotspotBanner(shard) {
    var container = document.getElementById("page-status-banner");
    clearChildren(container);
    var statusText = "Status: " + shard.proof_status;
    if (shard.p1_reviewer) statusText += " · P1: " + shard.p1_reviewer;
    if (shard.p2_verifier) statusText += " · P2: " + shard.p2_verifier;
    var banner = makeEl("div", "status-banner", statusText);
    if (shard.proof_status === "verified") banner.classList.add("status-verified");
    if (shard.proof_status === "p1_proofed") banner.classList.add("status-p1");
    container.appendChild(banner);

    (shard.findings || []).forEach(function (f) {
      var item = makeEl("div", "status-banner", "[" + f.severity.toUpperCase() + "] " + f.code + ": " + f.message);
      container.appendChild(item);
    });
  }

  function renderEpistolaryBoundaryCheck(shard) {
    var box = document.getElementById("epistolary-boundary-list");
    clearChildren(box);
    var letters = shard.letters_on_page || [];
    if (letters.length === 0) {
      box.appendChild(makeEl("div", null, "No letter start/end boundaries flagged on this leaf. Use block Transition selector above to split or enclose a letter."));
      return;
    }
    letters.forEach(function (ltr) {
      var row = makeEl("div", null, "Letter #" + ltr.sequence_index + ": " + (ltr.sender || "—") + " → " + (ltr.recipient || "—") + " [" + (ltr.dateline || "no date") + "] (" + ltr.confidence + ")");
      box.appendChild(row);
    });
  }

  function renderBlocksEditor(shard) {
    var container = document.getElementById("blocks-container");
    clearChildren(container);
    state.editsByBlock = {};
    state.activeTextarea = null;

    shard.blocks.forEach(function (blk, idx) {
      var card = makeEl("div", "block-card");
      var head = makeEl("div", "block-card-head");
      head.appendChild(makeEl("span", null, blk.block_id + " · #" + blk.reading_order));

      var controlsWrap = makeEl("div", "studio-controls");
      var typeSel = makeEl("select");
      state.manifest.block_types.forEach(function (bt) {
        var opt = makeEl("option", null, bt);
        opt.value = bt;
        if (bt === blk.type) opt.selected = true;
        typeSel.appendChild(opt);
      });

      var alignSel = makeEl("select");
      state.manifest.alignments.forEach(function (al) {
        var opt = makeEl("option", null, "align:" + al);
        opt.value = al;
        if (al === blk.alignment) opt.selected = true;
        alignSel.appendChild(opt);
      });

      var transSel = makeEl("select");
      ["none", "starts_new_letter", "starts_enclosure", "ends_enclosure"].forEach(function (tr) {
        var opt = makeEl("option", null, "trans:" + tr);
        opt.value = tr;
        if (tr === blk.epistolary_transition) opt.selected = true;
        transSel.appendChild(opt);
      });

      var noteInput = makeEl("input", "note-label-input");
      noteInput.type = "text";
      noteInput.placeholder = "note label";
      noteInput.setAttribute("aria-label", "Footnote label");
      noteInput.value = blk.note_label || "";

      controlsWrap.appendChild(typeSel);
      controlsWrap.appendChild(alignSel);
      controlsWrap.appendChild(transSel);
      controlsWrap.appendChild(noteInput);
      head.appendChild(controlsWrap);
      card.appendChild(head);

      var ta = makeEl("textarea", "block-textarea");
      ta.value = blk.tagged_text;
      ta.addEventListener("focus", function () {
        state.activeBlockIndex = idx;
        state.activeTextarea = ta;
        document.querySelectorAll(".block-card").forEach(function (c) { c.classList.remove("active-card"); });
        card.classList.add("active-card");
        updateBboxHighlight(blk.bbox);
      });

      function recordChange() {
        var cleanLabel = noteInput.value.trim() || null;
        state.editsByBlock[blk.block_id] = {
          original: blk,
          tagged_text: ta.value,
          type: typeSel.value,
          alignment: alignSel.value,
          transition: transSel.value,
          note_label: cleanLabel
        };
        if (transSel.value === "starts_new_letter" && blk.epistolary_transition !== "starts_new_letter") {
          state.forceSplits[blk.block_id] = true;
          delete state.forbidSplits[blk.block_id];
        } else {
          delete state.forceSplits[blk.block_id];
          if (blk.epistolary_transition === "starts_new_letter" && transSel.value === "none") {
            state.forbidSplits[blk.block_id] = true;
          } else {
            delete state.forbidSplits[blk.block_id];
          }
        }
      }

      ta.addEventListener("input", recordChange);
      typeSel.addEventListener("change", recordChange);
      alignSel.addEventListener("change", recordChange);
      transSel.addEventListener("change", recordChange);
      noteInput.addEventListener("input", recordChange);

      card.appendChild(ta);
      container.appendChild(card);
    });
  }

  function loadPage(pageNumber) {
    state.currentPage = pageNumber;
    state.forceSplits = {};
    state.forbidSplits = {};
    state.leaseToken = null;
    state.baseVersion = 0;
    var sel = document.getElementById("page-selector");
    if (sel) sel.value = String(pageNumber);
    window.location.hash = "page-" + pageNumber;
    updateBboxHighlight(null);

    fetch("pages/p" + pad4(pageNumber) + ".json")
      .then(function (r) { return r.json(); })
      .then(function (shard) {
        state.shard = shard;
        state.baseVersion = Number(shard.version || 0);
        renderScanView(shard);
        renderHotspotBanner(shard);
        renderBlocksEditor(shard);
        renderEpistolaryBoundaryCheck(shard);
      })
      .catch(function () {
        var msg = document.getElementById("signoff-feedback");
        if (msg) msg.textContent = "Unable to load page shard p" + pad4(pageNumber) + ".json";
      });
  }

  function jumpNextHotspot() {
    if (!state.manifest) return;
    var pages = state.manifest.pages || [];
    var total = pages.length;
    for (var offset = 1; offset <= total; offset++) {
      var candidate = pages[(state.currentPage - 1 + offset) % total];
      if (candidate && candidate.hotspot_count > 0) {
        loadPage(candidate.page_number);
        return;
      }
    }
  }

  async function buildProposalPayload(roundTarget) {
    var feedback = document.getElementById("signoff-feedback");
    feedback.textContent = "";
    var reviewerInput = document.getElementById("reviewer-handle");
    var reviewer = (reviewerInput.value || "").trim();
    if (!reviewer) {
      feedback.textContent = "Enter your proofreader handle before signing off.";
      return null;
    }

    var shard = state.shard;
    if (!shard) return null;

    var corrections = [];
    var currentBlocksJoined = [];
    shard.blocks.forEach(function (blk) {
      var edit = state.editsByBlock[blk.block_id];
      var curText = edit ? edit.tagged_text : blk.tagged_text;
      var curType = edit ? edit.type : blk.type;
      var curAlign = edit ? edit.alignment : blk.alignment;
      var curTrans = edit ? edit.transition : blk.epistolary_transition;
      var curNoteLabel = edit ? edit.note_label : (blk.note_label || null);
      if (curType !== "scanner_residue" && curType !== "noise") {
        currentBlocksJoined.push(curType + ":" + curAlign + ":" + curTrans + ":" + curText);
      }

      if (edit && (curText !== blk.tagged_text || curType !== blk.type || curAlign !== blk.alignment || curTrans !== blk.epistolary_transition || curNoteLabel !== (blk.note_label || null))) {
        var op = curText !== blk.tagged_text
          ? "set_text"
          : (curType !== blk.type
            ? "set_type"
            : (curAlign !== blk.alignment
              ? "set_alignment"
              : (curNoteLabel !== (blk.note_label || null) ? "set_note_label" : "set_transition")));
        corrections.push({
          correction_id: "corr-" + blk.block_id + "-" + Date.now(),
          page_number: shard.page_number,
          block_id: blk.block_id,
          anchor_text: (blk.plain_text || "").slice(0, 120),
          op: op,
          replacement_tagged_text: curText,
          replacement_type: curType,
          replacement_alignment: curAlign,
          replacement_transition: curTrans,
          replacement_note_label: curNoteLabel,
          reviewer: reviewer,
          source: "human"
        });
      }
    });

    var priorP1 = shard.p1_reviewer || "";
    var finalStatus = roundTarget;
    if (roundTarget === "verified") {
      if (!priorP1) {
        feedback.textContent = "Round P1 must be completed before Round P2 verification.";
        return null;
      }
      if (priorP1.toLowerCase() === reviewer.toLowerCase()) {
        feedback.textContent = "Two-person rule: P2 verifier (" + reviewer + ") must differ from P1 proofreader (" + priorP1 + ").";
        return null;
      }
      if (corrections.length > 0) {
        finalStatus = "p1_proofed";
        feedback.textContent = "Edits detected during P2 check; saving as new P1 text so an independent P2 can verify.";
      }
    }

    var textSha = await sha256Hex(currentBlocksJoined.join("\\n"));
    if (!textSha) {
      feedback.textContent = "WebCrypto SHA-256 unavailable (requires HTTPS or localhost); refusing unverified sign-off.";
      return null;
    }
    var forceIds = Object.keys(state.forceSplits);
    var forbidIds = Object.keys(state.forbidSplits);
    var letterMapPatch = (forceIds.length || forbidIds.length)
      ? { force_split_before_block_ids: forceIds, forbid_split_before_block_ids: forbidIds }
      : null;

    var proposal = {
      version: "correction-v2",
      book_id: state.manifest.book_id,
      run_id: state.manifest.active_run_id,
      reviewer: reviewer,
      created_at: new Date().toISOString(),
      corrections: corrections,
      page_signoff: {
        page_number: shard.page_number,
        status: finalStatus,
        reviewer: reviewer,
        p1_reviewer: finalStatus === "verified" ? priorP1 : reviewer,
        source_page_sha256: shard.source_page_sha256 || "",
        page_text_sha256: textSha,
        verified_at: new Date().toISOString()
      }
    };
    if (letterMapPatch) {
      proposal.letter_map_patch = letterMapPatch;
    }
    return proposal;
  }

  function buildRelayHeaders() {
    var headers = { "Content-Type": "application/json" };
    var tokenEl = document.getElementById("reviewer-token");
    var tok = tokenEl ? String(tokenEl.value || "").trim() : "";
    if (tok) {
      headers["Authorization"] = "Bearer " + tok;
    }
    return headers;
  }

  async function syncRelayPageVersion(baseUrl) {
    try {
      var stResp = await fetch(baseUrl + "/api/status?book_id=" + encodeURIComponent(state.manifest.book_id));
      if (!stResp.ok) return;
      var stData = await stResp.json();
      var hit = (stData.pages || []).find(function (p) { return Number(p.page_number) === Number(state.currentPage); });
      if (hit) {
        state.baseVersion = Number(hit.version || 0);
        if (state.shard) {
          state.shard.version = state.baseVersion;
          if (hit.p1_reviewer_id) state.shard.p1_reviewer = hit.p1_reviewer_id;
          if (hit.p2_verifier_id) state.shard.p2_verifier = hit.p2_verifier_id;
          if (hit.status) state.shard.proof_status = hit.status;
          renderHotspotBanner(state.shard);
        }
      }
    } catch (_err) {
      // Offline or relay unavailable; handled on claim/submit
    }
  }

  async function claimCurrentPage() {
    var feedback = document.getElementById("signoff-feedback");
    var relayUrl = state.manifest ? state.manifest.relay_url : "";
    if (!relayUrl || !state.shard) {
      if (feedback) feedback.textContent = "Relay URL not configured; using offline proposal.json mode.";
      return null;
    }
    var reviewer = ((document.getElementById("reviewer-handle") || {}).value || "").trim();
    if (!reviewer) {
      feedback.textContent = "Enter your proofreader handle before checking out a leaf.";
      return null;
    }
    var baseUrl = relayUrl.replace(/\\/$/, "");
    await syncRelayPageVersion(baseUrl);
    var resp = await fetch(baseUrl + "/api/claim", {
      method: "POST",
      headers: buildRelayHeaders(),
      body: JSON.stringify({
        book_id: state.manifest.book_id,
        page_number: state.currentPage,
        reviewer_id: reviewer
      })
    });
    var res = await resp.json();
    if (!resp.ok || !res.ok || !res.lease_token) {
      throw new Error(res.error || ("Claim failed (HTTP " + resp.status + ")"));
    }
    state.leaseToken = res.lease_token;
    feedback.textContent = "Leaf checked out (version=" + state.baseVersion + ").";
    return res.lease_token;
  }

  async function downloadProposal(roundTarget) {
    var proposal = await buildProposalPayload(roundTarget);
    if (!proposal) return;
    var blob = new Blob([JSON.stringify(proposal, null, 2) + "\\n"], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = makeEl("a");
    a.setAttribute("href", url);
    a.setAttribute("download", "proposal-" + proposal.book_id + "-p" + pad4(proposal.page_signoff.page_number) + ".json");
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    document.getElementById("signoff-feedback").textContent = "Downloaded proposal.json (" + proposal.page_signoff.status + ").";
  }

  async function submitToRelay(roundTarget) {
    var relayUrl = state.manifest ? state.manifest.relay_url : "";
    var feedback = document.getElementById("signoff-feedback");
    if (!relayUrl) {
      await downloadProposal(roundTarget);
      return;
    }
    var baseUrl = relayUrl.replace(/\\/$/, "");
    try {
      await syncRelayPageVersion(baseUrl);
      var proposal = await buildProposalPayload(roundTarget);
      if (!proposal) return;
      if (!state.leaseToken) {
        await claimCurrentPage();
      }
      proposal.lease_token = state.leaseToken;
      proposal.base_version = state.baseVersion;
      var resp = await fetch(baseUrl + "/api/proposal", {
        method: "POST",
        headers: buildRelayHeaders(),
        body: JSON.stringify(proposal)
      });
      var res = await resp.json();
      if (!resp.ok || !res.ok) {
        if (resp.status === 409) state.leaseToken = null;
        feedback.textContent = "Relay error: " + (res.error || ("HTTP " + resp.status));
        return;
      }
      state.leaseToken = null;
      state.baseVersion = Number(res.version || state.baseVersion + 1);
      feedback.textContent = "Submitted to relay: status=" + res.status + " (v" + state.baseVersion + ")";
    } catch (err) {
      feedback.textContent = "Relay error (" + (err && err.message ? err.message : "unreachable") + "); downloading offline proposal.json instead.";
      await downloadProposal(roundTarget);
    }
  }

  function buildPalettes(diacritics) {
    var bar = document.getElementById("palette-bar");
    clearChildren(bar);
    var tags = [
      ["i", "<i>", "</i>"],
      ["b", "<b>", "</b>"],
      ["sc", "<sc>", "</sc>"],
      ["sup", "<sup>", "</sup>"],
      ["sub", "<sub>", "</sub>"],
      ["unclear", "<unclear>", "</unclear>"]
    ];
    tags.forEach(function (spec) {
      var btn = makeEl("button", null, "<" + spec[0] + ">");
      btn.type = "button";
      btn.addEventListener("click", function () {
        insertAtCursor(state.activeTextarea, spec[1], spec[2]);
      });
      bar.appendChild(btn);
    });
    diacritics.forEach(function (ch) {
      var btn = makeEl("button", null, ch);
      btn.type = "button";
      btn.addEventListener("click", function () {
        insertAtCursor(state.activeTextarea, ch, null);
      });
      bar.appendChild(btn);
    });
  }

  function parseInitialHashPage(maxPages) {
    var m = /^#(?:page|leaf)-?(\\d+)$/i.exec(window.location.hash || "");
    if (!m) return 1;
    var n = parseInt(m[1], 10);
    return Math.max(1, Math.min(maxPages, n));
  }

  function initStudio() {
    fetch("manifest.json")
      .then(function (r) { return r.json(); })
      .then(function (manifest) {
        state.manifest = manifest;
        var sel = document.getElementById("page-selector");
        clearChildren(sel);
        (manifest.pages || []).forEach(function (p) {
          var badge = p.proof_status === "verified" ? " [P2✓]" : (p.proof_status === "p1_proofed" ? " [P1]" : "");
          var hs = p.hotspot_count > 0 ? (" · " + p.hotspot_count + "!") : "";
          var opt = makeEl("option", null, "Leaf " + p.page_number + " (p. " + p.folio + ")" + badge + hs);
          opt.value = String(p.page_number);
          sel.appendChild(opt);
        });
        buildPalettes(manifest.diacritics || []);
        var startP = parseInitialHashPage((manifest.pages || []).length || 1);
        if (manifest.pages && manifest.pages.length > 0) {
          var knownNums = manifest.pages.map(function (x) { return x.page_number; });
          if (knownNums.indexOf(startP) === -1) startP = knownNums[0];
          loadPage(startP);
        }
      });

    document.getElementById("page-selector").addEventListener("change", function (ev) {
      loadPage(parseInt(ev.target.value, 10));
    });
    document.getElementById("btn-prev-page").addEventListener("click", function () {
      if (!state.manifest) return;
      var nums = state.manifest.pages.map(function (x) { return x.page_number; });
      var idx = nums.indexOf(state.currentPage);
      if (idx > 0) loadPage(nums[idx - 1]);
    });
    document.getElementById("btn-next-page").addEventListener("click", function () {
      if (!state.manifest) return;
      var nums = state.manifest.pages.map(function (x) { return x.page_number; });
      var idx = nums.indexOf(state.currentPage);
      if (idx >= 0 && idx + 1 < nums.length) loadPage(nums[idx + 1]);
    });
    document.getElementById("btn-hotspot").addEventListener("click", jumpNextHotspot);
    document.getElementById("btn-zoom-in").addEventListener("click", function () { setZoom(state.zoomIndex + 1); });
    document.getElementById("btn-zoom-out").addEventListener("click", function () { setZoom(state.zoomIndex - 1); });
    document.getElementById("btn-p1-save").addEventListener("click", function () { submitToRelay("p1_proofed"); });
    document.getElementById("btn-p2-verify").addEventListener("click", function () { submitToRelay("verified"); });
    document.getElementById("btn-claim-page").addEventListener("click", function () {
      claimCurrentPage().catch(function (err) {
        var msg = document.getElementById("signoff-feedback");
        if (msg) msg.textContent = "Checkout error: " + (err && err.message ? err.message : "failed");
      });
    });
    document.getElementById("btn-export-json").addEventListener("click", function () { downloadProposal("p1_proofed"); });

    document.addEventListener("keydown", function (ev) {
      if (ev.altKey && (ev.key === "h" || ev.key === "H")) {
        ev.preventDefault();
        jumpNextHotspot();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initStudio);
})();
"""


def _build_studio_index_html(
    ast: CollectionAST,
    *,
    has_book_web: bool,
    has_letters: bool,
    relay_url: str = "",
) -> str:
    web_link = '<a href="../web/index.html">Book Edition</a>' if has_book_web else ""
    letters_link = '<a href="../letters/index.html">Letters</a>' if has_letters else ""
    page_ids_hidden = "".join(
        f'<span id="page-{p.page_number}" hidden="hidden"></span>' for p in ast.pages
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{html.escape(_build_proofing_csp(relay_url), quote=True)}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Proofing Studio — {html.escape(ast.title)}</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  {page_ids_hidden}
  <header class="studio-topbar">
    <div class="studio-brand">{html.escape(ast.title)} · Community Proofing Studio (P1 → P2)</div>
    <div class="studio-controls">
      <button type="button" id="btn-prev-page">← Prev</button>
      <select id="page-selector" aria-label="Select leaf"></select>
      <button type="button" id="btn-next-page">Next →</button>
      <button type="button" id="btn-hotspot" title="Alt+H">Next Hotspot (Alt+H)</button>
      {web_link}
      {letters_link}
    </div>
  </header>
  <main class="studio-grid">
    <section class="scan-pane" aria-label="Source scan facsimile">
      <div class="scan-toolbar">
        <span id="scan-caption">Loading scan...</span>
        <div class="studio-controls">
          <button type="button" id="btn-zoom-out">−</button>
          <span id="zoom-label">100%</span>
          <button type="button" id="btn-zoom-in">+</button>
        </div>
      </div>
      <div class="scan-scroll" id="osd-hook">
        <div class="scan-stage zoom-100" id="scan-stage">
          <img id="scan-image" alt="Archival scan leaf" />
          <svg class="bbox-overlay" viewBox="0 0 1000 1000" preserveAspectRatio="none">
            <rect id="active-bbox-rect" class="bbox-rect bbox-hidden" x="0" y="0" width="0" height="0" />
          </svg>
        </div>
      </div>
    </section>
    <section class="editor-pane" aria-label="Diplomatic transcription &amp; epistolary check">
      <div class="palette-bar" id="palette-bar" role="toolbar" aria-label="Constrained tags and diacritics"></div>
      <div class="editor-scroll">
        <div id="page-status-banner"></div>
        <div id="blocks-container"></div>
      </div>
      <div class="epistolary-panel">
        <div class="panel-title">Epistolary Boundary &amp; Metadata Check</div>
        <div id="epistolary-boundary-list"></div>
      </div>
      <div class="signoff-panel">
        <div class="panel-title">Two-Round Sign-Off (Type What the Printer Printed)</div>
        <div class="signoff-row">
          <input type="text" id="reviewer-handle" placeholder="Proofreader handle (e.g. c_asha)" aria-label="Proofreader handle" />
          <input type="password" id="reviewer-token" placeholder="Relay Bearer token (online)" aria-label="Relay session token" />
          <button type="button" id="btn-claim-page">Checkout Leaf</button>
          <button type="button" class="primary-btn" id="btn-p1-save">Save Round P1 (p1_proofed)</button>
          <button type="button" id="btn-p2-verify">Verify Round P2 (verified · P2 ≠ P1)</button>
          <button type="button" id="btn-export-json">Download proposal.json</button>
        </div>
        <div class="feedback-msg" id="signoff-feedback" role="status"></div>
      </div>
    </section>
  </main>
  <script src="app.js" defer></script>
</body>
</html>
"""


def render_proofing_studio(
    ast: CollectionAST,
    edition_dir: str | Path,
    *,
    proof_ledger: dict[str, Any] | None = None,
    run_dir: str | Path | None = None,
    source_pdf: str | Path | None = None,
    bundle_scans: bool = False,
    relay_url: str = "",
    has_book_web: bool = True,
    has_letters: bool = True,
) -> dict[str, Any]:
    """Render Safe-DOM Distributed Proofreaders (`P1 -> P2`) Studio into `<edition_dir>/proofing/`."""
    ed_root = Path(edition_dir)
    proof_dir = ed_root / "proofing"
    pages_dir = proof_dir / "pages"
    scans_dir = proof_dir / "scans"
    pages_dir.mkdir(parents=True, exist_ok=True)

    should_bundle = bundle_scans or (not bool(ast.archive_id))
    bundled_scans = (
        _bundle_local_scans(
            ast,
            scans_dir,
            run_dir=Path(run_dir) if run_dir else None,
            source_pdf=Path(source_pdf) if source_pdf else None,
        )
        if should_bundle
        else _collect_existing_local_scans(scans_dir)
    )

    ledger_pages = (proof_ledger or {}).get("pages") or {}
    findings_by_page: dict[int, list[dict[str, Any]]] = {}
    for finding in ast.findings:
        if finding.page_number is not None:
            findings_by_page.setdefault(finding.page_number, []).append(finding.model_dump(mode="json"))

    letters_by_page: dict[int, list[dict[str, Any]]] = {}
    for ltr in ast.letters:
        for pnum in range(ltr.start_page, ltr.end_page + 1):
            letters_by_page.setdefault(pnum, []).append(
                {
                    "letter_id": ltr.letter_id,
                    "sequence_index": ltr.sequence_index,
                    "sender": ltr.metadata.sender_display,
                    "recipient": ltr.metadata.recipient_display,
                    "dateline": ltr.metadata.dateline_plain,
                    "confidence": ltr.boundary_confidence,
                }
            )

    pages_to_emit = list(ast.pages)
    if not pages_to_emit and ast.total_pages > 0:
        pages_to_emit = [PageIR(page_number=p) for p in range(1, ast.total_pages + 1)]

    page_summaries: list[dict[str, Any]] = []
    for page in pages_to_emit:
        pnum = page.page_number
        shard, summary = _build_page_shard(
            page,
            page_letters=letters_by_page.get(pnum, []),
            page_findings=findings_by_page.get(pnum, []),
            ledger_entry=ledger_pages.get(str(pnum)) or {},
            local_scan=bundled_scans.get(pnum),
            archive_id=ast.archive_id,
        )
        atomic_write_json(pages_dir / f"p{pnum:04d}.json", shard)
        page_summaries.append(summary)

    from lanterns.core.models import BLOCK_ALIGNMENTS, BLOCK_TYPES

    manifest = {
        "schema_version": "1.0.0",
        "book_id": ast.book_id,
        "title": ast.title,
        "active_run_id": ast.active_run_id,
        "archive_id": ast.archive_id,
        "relay_url": relay_url,
        "diacritics": list(DIACRITIC_PALETTE),
        "block_types": list(BLOCK_TYPES),
        "alignments": list(BLOCK_ALIGNMENTS),
        "pages": page_summaries,
    }
    atomic_write_json(proof_dir / "manifest.json", manifest)
    atomic_write_text(proof_dir / "styles.css", PROOFING_CSS)
    atomic_write_text(proof_dir / "app.js", PROOFING_JS)
    atomic_write_text(
        proof_dir / "index.html",
        _build_studio_index_html(ast, has_book_web=has_book_web, has_letters=has_letters, relay_url=relay_url),
    )
    return {
        "proofing_index": str(proof_dir / "index.html"),
        "manifest_path": str(proof_dir / "manifest.json"),
        "shard_count": len(page_summaries),
        "bundled_scans": len(bundled_scans),
    }
