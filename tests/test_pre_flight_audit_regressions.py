"""Regression tests for zero-trust pre-flight audit fixes in Paper Lanterns v2."""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from lanterns.cli import _cmd_fetch_archive
from lanterns.core.config import BookConfig
from lanterns.core.models import PageExtractWire, WireBlock
from lanterns.core.paths import RepoPaths
from lanterns.epistolary.metadata import detect_scripts_and_orthography
from lanterns.epistolary.segmentation import segment_epistolary_collection
from lanterns.ops.catalog_status import garbage_collect_workspace
from lanterns.ops.doctor import run_doctor
from lanterns.providers.base import ProviderError, ProviderErrorKind, ProviderRequest, ProviderSuccess
from lanterns.providers.router import FallbackRouter
from lanterns.source.archive_org import (
    _pick_jp2_zip,
    access_leaves,
    build_originals_pdf_from_jp2_zip,
)
from tests.conftest import make_block


def test_fetch_archive_updates_book_toml_and_resolve_source_pdf_fallback(tmp_path: Path, monkeypatch) -> None:
    repo = RepoPaths.from_root(tmp_path)
    (repo.books_dir / "test-letter-vol").mkdir(parents=True)
    cfg = BookConfig(
        book_id="test-letter-vol",
        title="Test Letters Vol. 1",
        archive_id="letters_item_01",
        source_pdf="",
    )
    cfg.save(paths=repo)

    out_pdf = repo.sources_dir / "letters_item_01" / "letters_item_01.originals.pdf"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page(width=400, height=600)
    doc.save(out_pdf)
    doc.close()

    def _fake_download(identifier: str, dest_dir: Path, **_kwargs):
        return {
            "identifier": identifier,
            "mode": "jp2_zip",
            "primary": {"path": str(dest_dir / f"{identifier}_jp2.zip"), "sha256": "ziphash"},
            "originals_pdf": {"path": str(out_pdf), "sha256": "deadbeef1234", "page_count": 1},
        }

    monkeypatch.setattr("lanterns.source.archive_org.download_archive_source", _fake_download)
    args = argparse.Namespace(identifier="test-letter-vol", dest_dir=None, prefer_pdf=False, leaves=None, stream_slice=False)
    rc = _cmd_fetch_archive(args, repo)
    assert rc == 0

    reloaded = BookConfig.load("test-letter-vol", paths=repo)
    assert reloaded.source_pdf == "sources/letters_item_01/letters_item_01.originals.pdf"
    assert reloaded.source_pdf_sha256 == "deadbeef1234"
    resolved = reloaded.resolve_source_pdf_path(paths=repo)
    assert resolved == out_pdf

    # Even when source_pdf is blank on disk, fallback resolution locates sources/<archive_id>/<archive_id>.originals.pdf
    blank_cfg = BookConfig(book_id="test-letter-vol", title="Test Letters", archive_id="letters_item_01", source_pdf="")
    assert blank_cfg.resolve_source_pdf_path(paths=repo) == out_pdf


def test_access_leaves_aligns_zero_indexed_xml_when_expected_none_or_direct_len_matches() -> None:
    jp2s = ["b_0001.jp2", "b_0002.jp2", "b_0003.jp2", "b_0004.jp2"]
    scandata = """
    <bookData><pageData>
      <page leafNum="0"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="1"><addToAccessFormats>false</addToAccessFormats></page>
      <page leafNum="2"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="3"><addToAccessFormats>true</addToAccessFormats></page>
    </pageData></bookData>
    """
    expected = ["b_0001.jp2", "b_0003.jp2", "b_0004.jp2"]
    assert access_leaves(jp2s, scandata, expected_page_count=None) == expected
    assert access_leaves(jp2s, scandata, expected_page_count=3) == expected

    # When Scribe omitted leafNum="0" color card from ZIP (5 XML leaves vs 4 files starting at _0001), do not drop _0001
    scandata_no_0 = """
    <bookData><pageData>
      <page leafNum="0"><addToAccessFormats>false</addToAccessFormats></page>
      <page leafNum="1"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="2"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="3"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="4"><addToAccessFormats>true</addToAccessFormats></page>
    </pageData></bookData>
    """
    assert access_leaves(jp2s, scandata_no_0, expected_page_count=None) == jp2s


def test_tif_zip_support_and_ocr_witness_layer_preserved(tmp_path: Path) -> None:
    files = [
        {"name": "letters02.pdf", "size": "1000"},
        {"name": "letters02_tif.zip", "size": "50000"},
    ]
    picked = _pick_jp2_zip("letters02", files)
    assert picked is not None and picked["name"] == "letters02_tif.zip"

    tif_zip = tmp_path / "letters02_tif.zip"
    with zipfile.ZipFile(tif_zip, "w") as zf:
        for idx in (1, 2):
            buf = io.BytesIO()
            Image.new("RGB", (200, 300), (250 - idx * 10, 245, 240)).save(buf, format="TIFF")
            zf.writestr(f"letters02_tif/letters02_{idx:04d}.tif", buf.getvalue())

    deriv_pdf = tmp_path / "letters02.pdf"
    doc = fitz.open()
    for idx in (1, 2):
        pg = doc.new_page(width=200, height=300)
        pg.insert_text((20, 40), f"Original EIC witness page {idx}", fontsize=10, render_mode=3)
    doc.save(deriv_pdf)
    doc.close()

    out_pdf = tmp_path / "letters02.originals.pdf"
    res = build_originals_pdf_from_jp2_zip(tif_zip, out_pdf, derivative_pdf_path=deriv_pdf)
    assert res["page_count"] == 2
    assert res["preserved_ocr_witness"] is True
    with fitz.open(out_pdf) as check_doc:
        assert "Original EIC witness page 1" in check_doc[0].get_text()
        assert "Original EIC witness page 2" in check_doc[1].get_text()


def test_segmentation_attaches_leading_margin_note_before_letter_header() -> None:
    blocks = [
        # Page 1 leading margin_note before Letter 1
        make_block("p0001_b001", 1, "margin_note", "1616. Jan."),
        make_block(
            "p0001_b002",
            1,
            "letter_header",
            "449. Roe at Ajmere to Smythe.",
            epistolary_transition="starts_new_letter",
        ),
        make_block("p0001_b003", 1, "body", "Our business proceeds well."),
        make_block("p0001_b004", 1, "signature", "Tho. Roe."),
        # Page 2 opening margin_note before Letter 2 (must NOT leak into Letter 1!)
        make_block("p0002_b001", 2, "margin_note", "1617. May 12."),
        make_block(
            "p0002_b002",
            2,
            "letter_header",
            "450. Thomas Kerridge at Surat to the East India Company.",
            epistolary_transition="starts_new_letter",
        ),
        make_block("p0002_b003", 2, "salutation", "Right Worshipful,"),
        make_block("p0002_b004", 2, "body", "Our last vnto you was of the 10th past."),
        make_block("p0002_b005", 2, "signature", "Tho. Kerridge."),
    ]
    sections, letters = segment_epistolary_collection(blocks)
    assert len(letters) == 2
    assert letters[0].block_ids == ["p0001_b001", "p0001_b002", "p0001_b003", "p0001_b004"]
    assert letters[1].block_ids[0] == "p0002_b001"
    assert "p0002_b001" not in letters[0].block_ids
    non_letter_ids = [b.block_id for s in sections for it in s.items if it.kind == "blocks" for b in it.blocks]
    assert "p0001_b001" not in non_letter_ids
    assert "p0002_b001" not in non_letter_ids

    # Also test same-page margin_note between signature of Letter 1 and letter_header of Letter 2
    same_page_blocks = [
        make_block("p0011_b001", 11, "letter_header", "10. A to B.", epistolary_transition="starts_new_letter"),
        make_block("p0011_b002", 11, "body", "Short note."),
        make_block("p0011_b003", 11, "signature", "Yours, A."),
        make_block("p0011_b004", 11, "margin_note", "1617. June 1."),
        make_block("p0011_b005", 11, "letter_header", "11. C to D.", epistolary_transition="starts_new_letter"),
        make_block("p0011_b006", 11, "body", "Another letter."),
    ]
    _, sp_letters = segment_epistolary_collection(same_page_blocks)
    assert len(sp_letters) == 2
    assert sp_letters[0].block_ids == ["p0011_b001", "p0011_b002", "p0011_b003"]
    assert sp_letters[1].block_ids[0] == "p0011_b004"


def test_download_archive_source_fetches_derivative_pdf_for_ocr_witness(tmp_path: Path, monkeypatch) -> None:
    from lanterns.source import archive_org as ao

    zip_path = tmp_path / "item_jp2.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (100, 150), (255, 255, 255)).save(buf, format="TIFF")
        zf.writestr("item_jp2/item_0001.tif", buf.getvalue())
    deriv_path = tmp_path / "item.pdf"
    d = fitz.open()
    d.new_page(width=100, height=150).insert_text((10, 20), "Witness OCR line", render_mode=3)
    d.save(deriv_path)
    d.close()

    monkeypatch.setattr(
        ao,
        "fetch_archive_metadata",
        lambda cid, **_kw: {
            "identifier": cid,
            "jp2_zip": {"name": "item_jp2.zip", "size": 100},
            "pdf": {"name": "item.pdf", "size": 50},
            "scandata": None,
            "ppi": 300,
            "jbig2_risk": False,
            "jbig2_risk_level": "none",
            "jbig2_risk_reasons": [],
        },
    )
    downloaded: list[str] = []

    def _fake_dl(cid: str, entry: dict, dest_root: Path, **_kw):
        downloaded.append(str(entry["name"]))
        p = dest_root / Path(str(entry["name"])).name
        p.parent.mkdir(parents=True, exist_ok=True)
        src = zip_path if p.name.endswith(".zip") else deriv_path
        p.write_bytes(src.read_bytes())
        return {"path": str(p), "sha256": "abc"}

    monkeypatch.setattr(ao, "_download_metadata_entry", _fake_dl)
    res = ao.download_archive_source("item", tmp_path / "dl", prefer_jp2=True)
    assert "item.pdf" in downloaded
    assert res["originals_pdf"]["preserved_ocr_witness"] is True


def test_metadata_detects_oriya_sinhala_and_modi() -> None:
    found = detect_scripts_and_orthography("Odia ଓଡ଼ିଆ Sinhala සිංහල Modi \U0001160e\U00011630")
    assert "Oriya" in found
    assert "Sinhala" in found
    assert "Modi" in found


def test_router_escalates_max_tokens_before_band_split(tmp_path: Path) -> None:
    img_path = tmp_path / "p1.png"
    Image.new("RGB", (400, 600), (255, 255, 255)).save(img_path)

    class _EscalateStub:
        name = "gemini"
        model = "gemini-3.8-flash"
        harness = "sdk"

        def __init__(self) -> None:
            self.max_tokens_seen: list[int] = []

        def generate(self, request: ProviderRequest):
            mx = int((request.settings or {}).get("max_output_tokens") or 16384)
            self.max_tokens_seen.append(mx)
            if mx < 32768:
                return ProviderError(ProviderErrorKind.MAX_TOKENS, "truncated at 16384")
            wire = PageExtractWire(
                printed_folio="10",
                blocks=[WireBlock(type="body", bbox=[50, 50, 900, 900], text="Recovered dense text.")],
            ).model_dump()
            return ProviderSuccess(
                data=wire,
                raw_response={},
                provenance={"provider": "gemini", "model": "gemini-3.8-flash", "cost_estimate_usd": 0.001},
            )

    stub = _EscalateStub()
    router = FallbackRouter(stub, band_split_dir=tmp_path / "bands")
    req = ProviderRequest(
        prompt="extract",
        schema_name="page_extract_wire",
        image_paths=[str(img_path)],
        settings={"max_output_tokens": 16384},
    )
    outcome = router.execute_page(req, page_number=10)
    assert not outcome.failed_closed
    assert stub.max_tokens_seen == [16384, 32768]
    assert outcome.attempts[-1].strategy == "tier2_max_tokens_retry"

    # Also verify 16384 -> 32768 -> 65536 escalation and same-tier 2-band split BEFORE downgraded model
    class _AltModelStub:
        name = "gemini"
        model = "gemini-3.6-flash"
        harness = "sdk"

        def __init__(self) -> None:
            self.called = False

        def generate(self, request):
            self.called = True
            return ProviderError(ProviderErrorKind.SCHEMA_INVALID, "should not be called before 2-band split")

    class _SplitAt32kStub:
        name = "gemini"
        model = "gemini-4-hydrogen"
        harness = "sdk"

        def __init__(self) -> None:
            self.max_tokens_seen: list[int] = []

        def generate(self, request):
            mx = int((request.settings or {}).get("max_output_tokens") or 16384)
            self.max_tokens_seen.append(mx)
            if len(self.max_tokens_seen) <= 3:
                return ProviderError(ProviderErrorKind.MAX_TOKENS, f"truncated at {mx}")
            wire = PageExtractWire(
                printed_folio="10",
                blocks=[WireBlock(type="body", bbox=[50, 50, 900, 900], text="Band split recovered.")],
            ).model_dump()
            return ProviderSuccess(
                data=wire,
                raw_response={},
                provenance={"provider": "gemini", "model": self.model, "cost_estimate_usd": 0.001},
            )

    alt = _AltModelStub()
    prim = _SplitAt32kStub()
    router2 = FallbackRouter(prim, fallback_providers=[alt], band_split_dir=tmp_path / "bands_64k")
    outcome2 = router2.execute_page(req, page_number=10)
    assert not outcome2.failed_closed
    assert alt.called is False
    assert prim.max_tokens_seen[:3] == [16384, 32768, 65536]
    assert outcome2.attempts[-1].strategy == "tier2_band_bottom"


def test_gc_skips_active_run_lock_and_batch_job_unless_forced(tmp_path: Path) -> None:
    repo = RepoPaths.from_root(tmp_path)
    locked_run = repo.runs_dir / "book-a" / "run-locked"
    (locked_run / "ir").mkdir(parents=True)
    (locked_run / "ir" / "pages.json").write_text(json.dumps({"pages": [{"page_number": 1}]}))
    (locked_run / "renders").mkdir(parents=True)
    (locked_run / "renders" / "page_0001.png").write_bytes(b"pngdata")
    (locked_run / "run.lock").write_text("pid=1234")

    batch_run = repo.runs_dir / "book-b" / "run-batch"
    (batch_run / "ir").mkdir(parents=True)
    (batch_run / "ir" / "pages.json").write_text(json.dumps({"pages": [{"page_number": 1}]}))
    (batch_run / "renders").mkdir(parents=True)
    (batch_run / "renders" / "page_0001.png").write_bytes(b"pngdata")
    (batch_run / "batch_jobs.json").write_text(json.dumps({"chunks": {"c1": {"status": "JOB_STATE_RUNNING"}}}))

    res_safe = garbage_collect_workspace(tmp_path, apply=True, force=False)
    assert len(res_safe["skipped_active"]) == 2
    assert (locked_run / "renders" / "page_0001.png").is_file()
    assert (batch_run / "renders" / "page_0001.png").is_file()

    res_forced = garbage_collect_workspace(tmp_path, apply=True, force=True)
    assert res_forced["skipped_active"] == []
    assert not (locked_run / "renders").exists()
    assert not (batch_run / "renders").exists()


def test_doctor_flags_unpriced_route_in_profiles(tmp_path: Path) -> None:
    src_root = Path(__file__).resolve().parents[1]
    for d in ("src", "schemas", "config", "prompts"):
        (tmp_path / d).mkdir(parents=True)
    for sf in (src_root / "schemas").glob("*.json"):
        (tmp_path / "schemas" / sf.name).write_text(sf.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "config" / "pricing.json").write_text(
        (src_root / "config" / "pricing.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    bad_profiles = {"bad": {"primary": "gemini/unpriced-model-xyz", "fallback_chain": []}}
    (tmp_path / "config" / "profiles.json").write_text(json.dumps(bad_profiles), encoding="utf-8")
    res = run_doctor(tmp_path, genai_version_override="3.3.0")
    assert res["ok"] is False
    assert any(i["check"] == "pricing_routes" for i in res["issues"])


def test_empty_accepted_blocks_guard_and_intake_line_wrap_hash(tmp_path: Path) -> None:
    from lanterns.epistolary.corrections import (
        apply_corrections_and_proof_overlay,
        compute_page_text_sha256,
        intake_proposal,
        load_proof_ledger,
        save_proof_ledger,
    )
    from lanterns.extraction.line_wraps import resolve_page_ir_line_wraps
    from tests.conftest import make_page

    repo = RepoPaths.from_root(tmp_path)
    (repo.books_dir / "ledger-guard-book").mkdir(parents=True)
    BookConfig(book_id="ledger-guard-book", title="Ledger Guard Test").save(paths=repo)

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "body", "We value correspondence and long correspondence."),
                make_block("p0001-b0002", 1, "body", "Our corres-\npondence continues."),
            ],
            printed_folio="1",
        )
    ]
    save_proof_ledger(
        "ledger-guard-book",
        {
            "schema_version": "1.0.0",
            "book_id": "ledger-guard-book",
            "pages": {
                "1": {
                    "page_number": 1,
                    "status": "p1_proofed",
                    "p1_reviewer_id": "alice",
                    "attestation": {"accepted_blocks": []},
                }
            },
        },
        repo_root=tmp_path,
    )
    overlaid, summary = apply_corrections_and_proof_overlay(pages, book_id="ledger-guard-book", repo_root=tmp_path)
    assert len(overlaid[0].blocks) == 2
    assert summary["p1_proofed"] == 1

    prop_path = tmp_path / "proposal.json"
    prop_path.write_text(
        json.dumps(
            {
                "version": "correction-v2",
                "book_id": "ledger-guard-book",
                "reviewer": "bob",
                "corrections": [],
                "page_signoff": {
                    "page_number": 1,
                    "status": "p1_proofed",
                    "reviewer": "bob",
                },
            }
        ),
        encoding="utf-8",
    )
    # Reset status to unproofed so intake records the signoff and hashes wrapped text
    save_proof_ledger(
        "ledger-guard-book",
        {"schema_version": "1.0.0", "book_id": "ledger-guard-book", "pages": {}},
        repo_root=tmp_path,
    )
    intake_proposal("ledger-guard-book", prop_path, repo_root=tmp_path, pages=pages)
    saved_rec = load_proof_ledger("ledger-guard-book", repo_root=tmp_path)["pages"]["1"]
    wrapped_pages, _ = resolve_page_ir_line_wraps(pages)
    expected_sha = compute_page_text_sha256(wrapped_pages[0].blocks)
    assert saved_rec["page_text_sha256"] == expected_sha


def test_proofing_scan_bundling_excludes_json_and_standalone_letters_include_drawer(tmp_path: Path) -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.renderers import render_complete_edition
    from lanterns.source.archive_org import build_iiif_page_url
    from tests.conftest import make_page

    assert build_iiif_page_url("my_item", 7, width=1000) == "https://archive.org/download/my_item/page/n7_w1000.jpg"

    run_dir = tmp_path / "run"
    (run_dir / "pages").mkdir(parents=True)
    # Minimal JSON file that older bundling accidentally copied into proofing/scans/
    (run_dir / "pages" / "page_0001.json").write_text("{}", encoding="utf-8")

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "My dear Friend, all is well."),
                make_block("p0001-b0003", 1, "dateline", "Calcutta, 1st May, 1810.", alignment="right"),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="scan-drawer-book", title="Scan Drawer Book", archive_id="arch_123")
    ed_dir = tmp_path / "edition"
    # Plant stale .json in scans/ to verify pruning too
    (ed_dir / "proofing" / "scans").mkdir(parents=True)
    (ed_dir / "proofing" / "scans" / "page_0001.json").write_text("{}", encoding="utf-8")

    render_complete_edition(ast, ed_dir, run_dir=run_dir, bundle_scans=True, include_proofing=True)
    assert not (ed_dir / "proofing" / "scans" / "page_0001.json").exists()
    shard = json.loads((ed_dir / "proofing" / "pages" / "p0001.json").read_text(encoding="utf-8"))
    assert not str(shard["scan_url"]).endswith(".json")
    assert "/page/n0_w" in str(shard["scan_url"])

    letter_html = (ed_dir / "letters" / f"{ast.letters[0].slug}.html").read_text(encoding="utf-8")
    assert 'id="scan-drawer"' in letter_html
    assert 'src="../web/reader.js"' in letter_html


def test_post_letter_observations_framing_and_bottom_dateline() -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from tests.conftest import make_page

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "We have dispatched the convoy."),
                make_block("p0001-b0003", 1, "signature", "TIPU SULTAN.", alignment="right"),
                make_block("p0001-b0004", 1, "dateline", "Putn, 17th February, 1785.", alignment="right"),
                make_block("p0001-b0005", 1, "heading", "OBSERVATIONS.", heading_level=2),
                make_block("p0001-b0006", 1, "editorial", "This dispatch relates to the Malabar frontier."),
                make_block("p0001-b0007", 1, "letter_header", "LETTER II", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0008", 1, "body", "Proceed with the artillery."),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="obs-framing-book", title="Observations Test")
    assert len(ast.letters) == 2
    assert "p0001-b0004" in ast.letters[0].block_ids
    assert ast.letters[0].metadata.dateline_position == "bottom"
    assert "p0001-b0005" in ast.letters[0].following_editorial_block_ids
    assert "p0001-b0006" in ast.letters[0].following_editorial_block_ids
    assert "p0001-b0005" not in ast.letters[1].preceding_editorial_block_ids
    assert "p0001-b0006" not in ast.letters[1].preceding_editorial_block_ids


def test_backmatter_page_endnotes_and_volume_legend_fallback() -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from tests.conftest import make_page

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
                make_block(
                    "p0001-b0002",
                    1,
                    "body",
                    "Sir Thomas Roe arrived at Ajmere<sup>1</sup> under royal<sup>*</sup> safe-conduct.",
                ),
            ],
            printed_folio="12",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "heading", "TABLE OF CONTENTS", heading_level=1),
                make_block("p0002-b0002", 2, "footnote", "* Asterisk denotes unpublished MS.", note_label="*"),
                make_block("p0002-b0003", 2, "heading", "NOTES.", heading_level=1),
                make_block("p0002-b0004", 2, "heading", "Page 12.", heading_level=2),
                make_block("p0002-b0005", 2, "footnote", "1 Sir Thomas Roe's embassy (1615–1618).", note_label="1"),
            ],
            printed_folio="200",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="endnotes-legend-book", title="Endnotes & Legend")
    fn_by_label = {fn.label: fn for fn in ast.footnotes.values()}
    assert fn_by_label["1"].is_orphan is False
    assert fn_by_label["1"].referenced_in_block_ids == ["p0001-b0002"]
    assert fn_by_label["*"].is_orphan is False
    assert fn_by_label["*"].referenced_in_block_ids == ["p0001-b0002"]


def test_header_metadata_parsing_and_glued_digit_cleaning() -> None:
    from lanterns.core.tags import strip_glued_footnote_digits
    from lanterns.epistolary.metadata import parse_header_metadata_lines

    assert strip_glued_footnote_digits("Sackea1 in 1616.1, I610") == "Sackea in 1616., I610"
    assert strip_glued_footnote_digits("John Tucker3") == "John Tucker"

    s1, r1, d1 = parse_header_metadata_lines(["383", "John Gourney to Wm. Nicholls at Achin, 23rd and\n28th July, 1616.<sup>2</sup>"])
    assert (s1, r1, d1) == ("John Gourney", "Wm. Nicholls at Achin", "23rd and 28th July, 1616.")

    s2, r2, d2 = parse_header_metadata_lines(["373\nThe King of Achin to the King of England."])
    assert (s2, r2, d2) == ("The King of Achin", "the King of England", None)

    s3, r3, d3 = parse_header_metadata_lines(["To Mirza Ali;(1) dated Putn, 17th February 1785."])
    assert (s3, r3, d3) == (None, "Mirza Ali", "Putn, 17th February 1785.")


def test_witness_inline_soft_wrap_and_catchword_deduplication(tmp_path: Path) -> None:
    from lanterns.extraction.line_wraps import (
        dedup_or_join_cross_page_tagged,
        resolve_page_ir_line_wraps,
    )
    from tests.conftest import make_page

    wdir = tmp_path / "witnesses"
    wdir.mkdir()
    (wdir / "page_0001.txt").write_text(
        "These commodities are shipped for Surat.\nAll commodities arrived, good commodities.",
        encoding="utf-8",
    )

    pages = [make_page(1, [make_block("p0001-b0001", 1, "body", "Sundry commo-dities and twenty-one bales.")])]
    resolved, _ = resolve_page_ir_line_wraps(pages, witness_dir=wdir)
    assert resolved[0].blocks[0].plain_text == "Sundry commodities and twenty-one bales."

    l_cat, r_cat = dedup_or_join_cross_page_tagged("accurately poised upon their Points.", "Points. Yet I grant", {}, {})
    assert (l_cat, r_cat) == ("accurately poised upon their", "Points. Yet I grant")

    l_rep, r_rep = dedup_or_join_cross_page_tagged("if I had", "had none at all", {}, {})
    assert (l_rep, r_rep) == ("if I had", "had none at all")


def test_tei_bottom_dateline_closer_and_list_table_pagebreaks(tmp_path: Path) -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.renderers.render_markdown_tei import render_tei_edition
    from tests.conftest import make_page

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "list_item", "First cargo item."),
                make_block("p0001-b0003", 1, "table_row", "Silk | 10 bales"),
                make_block("p0001-b0004", 1, "dateline", "Written this 4th May, 1756."),
            ],
            printed_folio="10",
        )
    ]
    ast = compile_collection_ast(pages, book_id="tei-closer-book", title="TEI Closer Test")
    ed_dir = tmp_path / "tei-out"
    render_tei_edition(ast, ed_dir)
    tei_xml = (ed_dir / "tei" / "tei-closer-book.tei.xml").read_text(encoding="utf-8")
    assert '<pb xml:id="page-1" n="10"/>' in tei_xml
    assert "<closer><dateline>Written this 4th May, 1756.</dateline></closer>" in tei_xml


def test_scan_check_does_not_silence_footnote_parity() -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.validation import run_ast_validators
    from tests.conftest import make_page

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "Unmatched note reference<sup>9</sup> in text."),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="scan-check-parity", title="Scan Check Parity")
    machine_ledger = {
        "schema_version": "1.0.0",
        "book_id": "scan-check-parity",
        "pages": {
            "1": {
                "page_number": 1,
                "status": "needs_review",
                "evidence": {"confirmed_block_ids": ["p0001-b0002"]},
            }
        },
    }
    findings_machine = run_ast_validators(ast, proof_ledger=machine_ledger)
    unmatched_machine = [f for f in findings_machine if f.code == "unmatched_callout"]
    assert unmatched_machine and unmatched_machine[0].severity == "warning"

    human_ledger = {
        "schema_version": "1.0.0",
        "book_id": "scan-check-parity",
        "pages": {
            "1": {
                "page_number": 1,
                "status": "p1_proofed",
                "p1_reviewer_id": "alice",
            }
        },
    }
    findings_human = run_ast_validators(ast, proof_ledger=human_ledger)
    unmatched_human = [f for f in findings_human if f.code == "unmatched_callout"]
    assert unmatched_human and unmatched_human[0].severity == "info"


def test_multiline_inline_tags_balanced_and_gate_rejects_escaped_tag_leak(tmp_path: Path) -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.renderers import render_complete_edition
    from lanterns.renderers.html_utils import render_inline_html
    from lanterns.renderers.render_markdown_tei import _render_tei_inline
    from lanterns.validation.compiled_gate import validate_compiled_edition
    from tests.conftest import make_page

    multiline_addr = "<i>For Mr.</i> <sc>Bentley,</sc> <i>at the Palace\nat Worceſter.</i>"
    html_out = render_inline_html(multiline_addr, preserve_line_breaks=True)
    assert "&lt;/i&gt;" not in html_out
    assert "<i>at the Palace</i><br />\n<i>at Worceſter.</i>" in html_out

    cascade_valediction = "<i>Your moſt\nhumble Servant\nto command,</i>"
    cascade_out = render_inline_html(cascade_valediction, preserve_line_breaks=True, cascading=True)
    assert "&lt;/i&gt;" not in cascade_out
    assert cascade_out.count("<i>") == 3 and cascade_out.count("</i>") == 3

    tei_out = _render_tei_inline(
        "<i>(2)\nTo Sj. Pramada</i>",
        block_footnotes=None,
        available_footnotes={},
        ref_registry={},
        seen_pb_pages=set(),
        preserve_lb=True,
    )
    assert "&lt;/i&gt;" not in tei_out
    assert '<hi rend="italic">(2)</hi><lb/><hi rend="italic">To Sj. Pramada</hi>' == tei_out

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "Clean text."),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="gate-escape-book", title="Gate Escape Test")
    ed_dir = tmp_path / "ed"
    render_complete_edition(ast, ed_dir, include_proofing=True)
    clean_gate = validate_compiled_edition(ed_dir, ast, require_proofing=True)
    assert clean_gate["error_count"] == 0

    sec_html = ed_dir / "web" / "sections" / "sec-01.html"
    sec_html.write_text(sec_html.read_text(encoding="utf-8") + "<p>Broken &lt;/i&gt;</p>", encoding="utf-8")
    dirty_gate = validate_compiled_edition(ed_dir, ast, require_proofing=True)
    assert dirty_gate["error_count"] >= 1


def test_ornamental_break_observations_attach_to_previous_letter() -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from tests.conftest import make_page

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "LETTER V", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "We march to Seringapatam."),
                make_block("p0001-b0003", 1, "ornamental_break", ""),
                make_block("p0001-b0004", 1, "heading", "OBSERVATIONS.", heading_level=2),
                make_block("p0001-b0005", 1, "editorial", "This relates to the Mysore treaty."),
                make_block("p0001-b0006", 1, "ornamental_break", ""),
                make_block("p0001-b0007", 1, "letter_header", "LETTER VI", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0008", 1, "body", "Send the camels forward."),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="ornament-obs-book", title="Ornament Obs Test")
    assert len(ast.letters) == 2
    assert "p0001-b0004" in ast.letters[0].following_editorial_block_ids
    assert "p0001-b0005" in ast.letters[0].following_editorial_block_ids
    assert "p0001-b0004" not in ast.letters[1].preceding_editorial_block_ids
    assert "p0001-b0005" not in ast.letters[1].preceding_editorial_block_ids


def test_early_modern_superscript_exclusions_and_header_month_parsing() -> None:
    from lanterns.core.tags import extract_inline_markers
    from lanterns.epistolary.metadata import parse_header_metadata_lines

    txt = (
        "Account: d<sup>o</sup>, D<sup>o</sup>, d.<sup>o</sup>, 537<sup>li</sup>, vi<sup>li</sup>, "
        "3/.<sup>s</sup>, 2<sub>2</sub><sup>1</sup>, <sub>4</sub><sup>1</sup>th, 26<sup>en</sup> a<sup>o</sup>, "
        "Geograph<sup>l</sup> Estab<sup>t</sup>, and date<sup>the 5th of the month Rubbâny</sup> note<sup>1</sup>."
    )
    assert extract_inline_markers(txt) == ["1"]

    s, r, d = parse_header_metadata_lines(
        ["A letter [from Sir Thomas Roe] to Erwin Rohde, Aug. 17, 1869."]
    )
    assert (s, r, d) == ("Sir Thomas Roe", "Erwin Rohde", "Aug. 17, 1869.")


def test_italic_catchword_dedup_and_long_paragraph_anchor_match() -> None:
    from lanterns.epistolary.corrections import apply_corrections_and_proof_overlay
    from lanterns.extraction.line_wraps import dedup_or_join_cross_page_tagged
    from tests.conftest import make_page

    l_it, r_it = dedup_or_join_cross_page_tagged("some <i>pagoda</i>", "<i>pagoda</i> of Mysore", {}, {})
    assert (l_it, r_it) == ("some", "<i>pagoda</i> of Mysore")

    long_raw = (
        "This is a very long opening sentence describing the corres-\npondence from Surat that "
        + ("exceeds four hundred characters of archival text in the raw JSON block. " * 6)
    )
    dehyph_anchor = (
        "This is a very long opening sentence describing the correspondence from Surat that "
        "exceeds four hundred characters of arch"
    )
    pages = [make_page(1, [make_block("p0001-b0001", 1, "body", long_raw)])]
    out_pages, summary = apply_corrections_and_proof_overlay(
        pages,
        book_id="long-anchor-book",
        corrections=[
            {
                "correction_id": "c-long-1",
                "page_number": 1,
                "block_id": "p0001-b0001",
                "anchor_text": dehyph_anchor,
                "op": "set_type",
                "replacement_type": "editorial",
            }
        ],
    )
    assert summary["applied_corrections"] == 1
    assert out_pages[0].blocks[0].type == "editorial"


def test_strict_regex_allowlist_and_explicit_letter_numeral_stems(tmp_path: Path) -> None:
    from lanterns.epistolary.metadata import _extract_letter_number
    from lanterns.validation.architecture_guard import run_architecture_guard
    from tests.test_doctor_and_security import _copy_minimal_repo_tree

    assert _extract_letter_number([make_block("b1", 1, "letter_header", "Charles X")]) is None
    assert _extract_letter_number([make_block("b2", 1, "letter_header", "Henry VIII")]) is None
    assert _extract_letter_number([make_block("b3", 1, "letter_header", "Part II")]) is None
    assert _extract_letter_number([make_block("b4", 1, "letter_header", "LETTER IV.")]) == "IV"
    assert _extract_letter_number([make_block("b5", 1, "letter_header", "Lettre IX")]) == "IX"

    fake_seg = _copy_minimal_repo_tree(tmp_path / "repo_strict_seg")
    (fake_seg / "src" / "lanterns" / "epistolary" / "_sneaky_seg.py").write_text(
        'import re\nSNEAKY = re.compile(r"\\bsalutation_word\\b")\n',
        encoding="utf-8",
    )
    res_seg = run_architecture_guard(fake_seg)
    assert res_seg["ok"] is False
    assert any(v["rule"] == "content_word_heuristic" for v in res_seg["violations"])

    fake_rend = _copy_minimal_repo_tree(tmp_path / "repo_strict_rend")
    (fake_rend / "src" / "lanterns" / "renderers" / "_sneaky_rend.py").write_text(
        'import re\nSNEAKY = re.compile(r"\\bsalutation_word\\b")\n',
        encoding="utf-8",
    )
    res_rend = run_architecture_guard(fake_rend)
    assert res_rend["ok"] is False
    assert any(v["rule"] == "content_word_heuristic" for v in res_rend["violations"])


def test_leader_dot_ditto_mille_margin_parity_and_cross_page_joins() -> None:
    from lanterns.core.tags import extract_inline_markers
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.epistolary.metadata import extract_epistolary_metadata
    from lanterns.extraction.line_wraps import dedup_or_join_cross_page_tagged
    from lanterns.validation.validators import footnote_parity
    from tests.conftest import make_page

    # 1. Leader-dot ....d<sup>o</sup>, digit-attached 50<sup>m</sup>, raised degree zero 170<sup>0</sup>, and epistolary abbrevs are non-callouts
    assert extract_inline_markers("peas)........ ¼....d<sup>o</sup> and 50<sup>m</sup><sup>1</sup> men") == ["1"]
    assert extract_inline_markers("a deviation of 170<sup>0</sup> and 75<sup>0</sup> in water<sup>1</sup>.") == ["1"]
    assert extract_inline_markers("D<sup>R.</sup> S., 6 Feb<sup>y</sup>, 22 Jan<sup>y</sup>, Oct<sup>r</sup>, Dec<sup>r</sup>, Rich<sup>d</sup>, Rev<sup>d</sup> Smith<sup>2</sup>") == ["2"]

    # 2. Same-page margin_note parity in footnote_parity & postscript/list_item cross-page hyphen joins
    p1 = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "letter_header", "William Edwards to the East India Company.", epistolary_transition="starts_new_letter"),
            make_block("p0001-b0002", 1, "dateline", "17th March, 1610."),
            make_block("p0001-b0003", 1, "body", "This is the 13th<sup>*</sup> day at sea."),
            make_block("p0001-b0004", 1, "margin_note", "* Note in margin.", note_label="*"),
            make_block("p0001-b0005", 1, "dateline", "From aboard, this 17th March, 1610."),
            make_block("p0001-b0006", 1, "valediction", "Yours faithfully"),
            make_block("p0001-b0007", 1, "signature", "Wm. Pemberton."),
            make_block("p0001-b0008", 1, "address_block", "Endorsed: Received the 11th of January."),
            make_block("p0001-b0009", 1, "postscript", "Also see other-", continues_to_next=True),
        ],
        printed_folio="1",
    )
    p2 = make_page(
        2,
        [
            make_block("p0002-b0001", 2, "body", "wise let us know.", continues_from_prev=True),
            make_block("p0002-b0002", 2, "caption", "FACSIMILE PLATE"),
            make_block("p0002-b0003", 2, "illustration", "[Plate]"),
        ],
        printed_folio="2",
    )
    ast = compile_collection_ast([p1, p2], book_id="hardening-test", title="Hardening Test")
    parity_findings = footnote_parity(ast)
    assert [f for f in parity_findings if f.code == "unmatched_callout"] == []
    assert len(ast.letters) == 1
    assert ast.letters[0].blocks[8].plain_text == "Also see otherwise"
    assert ast.letters[0].blocks[9].plain_text == "let us know."
    assert all(b.type not in {"caption", "illustration"} for b in ast.letters[0].blocks)

    # 3. hdr_recipient takes precedence over address_block docketing endorsement
    meta = extract_epistolary_metadata(ast.letters[0].blocks)
    assert meta.recipient_display == "the East India Company"

    # 4. Leading running quotation mark across page join & spaced-punctuation catchword
    from collections import Counter

    l_q, r_q = dedup_or_join_cross_page_tagged("by procras-", "“ tination of time", Counter(), Counter())
    assert (l_q, r_q) == ("by procrastination", "of time")
    l_c, r_c = dedup_or_join_cross_page_tagged("in the year 1165;", "1165 ; on the same day", Counter(), Counter())
    assert (l_c, r_c) == ("in the year", "1165 ; on the same day")


def test_archaic_ye_superscript_and_folio_prefixed_endnote_rows() -> None:
    from lanterns.core.tags import extract_inline_markers
    from lanterns.epistolary.compiler import compile_collection_ast
    from tests.conftest import make_page

    assert extract_inline_markers("draw on you y<sup>e</sup> trouble, pray S<sup>r</sup> do me y<sup>e</sup> Justice<sup>7</sup>") == ["7"]

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "<i>Joseph Addison to Chamberlain Dashwood</i>", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "You know y<sup>e</sup> story of Mr. Bays<sup>1</sup> and y<sup>t</sup> matter<sup>2</sup>."),
            ],
            printed_folio="9",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "heading", "NOTES", heading_level=1),
                make_block("p0002-b0002", 2, "footnote", "9 1. <b>Mr. Bays</b>: John Dryden."),
                make_block("p0002-b0003", 2, "footnote", "PAGE 9 2. Alluding to a famous verse:"),
                make_block("p0002-b0004", 2, "footnote", "\"First line of verse.\nSecond line of verse.\""),
            ],
            printed_folio="112",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="folio-endnote-rows-test", title="Folio Endnote Rows")
    assert [f for f in ast.findings if f.severity == "warning" or f.severity == "blocker"] == []
    fn_by_label = {fn.label: fn for fn in ast.footnotes.values()}
    assert fn_by_label["1"].origin_page == 1
    assert fn_by_label["1"].origin_folio == "9"
    assert fn_by_label["1"].is_orphan is False
    assert fn_by_label["2"].origin_page == 1
    assert "Second line of verse." in fn_by_label["2"].plain_text
    assert fn_by_label["2"].is_orphan is False


