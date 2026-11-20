"""Phase 0 regression tests covering security (C1), data integrity (C2/C3), proofing studio (H1), and CLI compile (H11)."""

from __future__ import annotations

import argparse
from collections import Counter
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image

from lanterns.cli import _cmd_benchmark, _cmd_compile
from lanterns.core.config import BookConfig, load_profiles
from lanterns.core.paths import RepoPaths
from lanterns.core.pricing import ensure_priced_routes
from lanterns.core.tags import promote_bare_symbol_in_tagged
from lanterns.epistolary.compiler import _promote_page_symbol_callouts, compile_collection_ast
from lanterns.epistolary.corrections import _merge_letter_map_patch, intake_proposal, load_letter_map
from lanterns.extraction.line_wraps import (
    DEHYPHENATABLE_PROSE_TYPES,
    dedup_or_join_cross_page_tagged,
    resolve_page_ir_line_wraps,
)
from lanterns.extraction.normalize import solve_monotonic_folios
from lanterns.extraction.pipeline import run_extraction
from lanterns.providers.base import MockProvider
from lanterns.providers.gemini_cli_harness import load_native_scan_check, scan_override_path
from lanterns.renderers import render_complete_edition
from lanterns.renderers.html_utils import _format_folio_html
from lanterns.renderers.proofing_studio import _build_proofing_csp, render_proofing_studio
from lanterns.renderers.render_book_web import READER_JS
from lanterns.source.archive_org import (
    _extract_jp2_slice_if_requested,
    access_leaves,
    build_iiif_info_url,
    build_iiif_page_url,
    resolve_raw_leaf_numbers,
)
from lanterns.validation.architecture_guard import evaluate_epistolary_exam
from lanterns.validation.validators import (
    diacritic_and_script_integrity,
    epistolary_boundary_anomalies,
    furniture_leak,
)
from tests.conftest import make_block, make_page

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_text_pdf(pdf_path: Path, texts: list[str]) -> None:
    doc = fitz.open()
    for txt in texts:
        pg = doc.new_page(width=400, height=600)
        pg.insert_text((40, 80), txt, fontsize=12)
    doc.save(pdf_path)
    doc.close()


def test_c1_folio_quote_escaping_csp_and_no_local_scan_404(tmp_path: Path) -> None:
    malicious_folio = '1" autofocus onfocus="alert(1)'
    web_span = _format_folio_html(1, malicious_folio, seen_page_ids=set(), proof_href_prefix="../proofing/index.html", epub=False)
    epub_span = _format_folio_html(1, malicious_folio, seen_page_ids=set(), proof_href_prefix=None, epub=True)
    assert 'onfocus="alert' not in web_span
    assert "&quot;" in web_span
    assert 'onfocus="alert' not in epub_span
    assert "&quot;" in epub_span

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", f'Text [p. {malicious_folio}] ends cleanly.'),
            ],
            printed_folio="1",
        )
    ]
    ast = compile_collection_ast(pages, book_id="csp-folio-test", title="CSP Test", archive_id="ia_item_1")
    ed_dir = tmp_path / "edition"
    render_complete_edition(ast, ed_dir, bundle_scans=False, include_proofing=True)

    sec_html = next((ed_dir / "web" / "sections").glob("*.html")).read_text(encoding="utf-8")
    web_idx = (ed_dir / "web" / "index.html").read_text(encoding="utf-8")
    ltr_html = (ed_dir / "letters" / f"{ast.letters[0].slug}.html").read_text(encoding="utf-8")
    ltr_idx = (ed_dir / "letters" / "index.html").read_text(encoding="utf-8")
    for doc_html in (sec_html, web_idx, ltr_html, ltr_idx):
        assert 'http-equiv="Content-Security-Policy"' in doc_html

    assert 'data-has-local-scans="0"' in sec_html
    assert 'data-has-local-scans="0"' in ltr_html
    assert 'data-has-local-scans' in READER_JS

    # EPUB XHTML must parse cleanly with quote-escaped folio attributes
    epub_path = ed_dir / "epub" / "csp-folio-test.epub"
    with zipfile.ZipFile(epub_path, "r") as zf:
        import xml.etree.ElementTree as ET

        for name in zf.namelist():
            if name.endswith(".xhtml"):
                ET.fromstring(zf.read(name))


def test_c2_partial_pages_extraction_preserves_untargeted_pages(tmp_path: Path) -> None:
    repo = RepoPaths.from_root(tmp_path)
    (tmp_path / "schemas").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "schemas" / "block_ir.schema.json", tmp_path / "schemas" / "block_ir.schema.json")
    shutil.copy2(REPO_ROOT / "schemas" / "page_extract_wire.schema.json", tmp_path / "schemas" / "page_extract_wire.schema.json")
    (tmp_path / "prompts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "prompts" / "extract_epistolary_v1.md", tmp_path / "prompts" / "extract_epistolary_v1.md")

    pdf_path = tmp_path / "sources" / "sample.pdf"
    pdf_path.parent.mkdir(parents=True)
    _make_text_pdf(pdf_path, ["Letter I\nDear Sir,\nPage 1", "Body page 2 continued.", "Yours,\nA. Page 3."])

    BookConfig(
        book_id="partial-book",
        title="Partial Book",
        collection="world-correspondence",
        source_pdf="sources/sample.pdf",
    ).save(paths=repo)

    def _wire_for(label: str) -> dict[str, Any]:
        return {
            "printed_folio": label,
            "page_orientation": "upright",
            "warning_codes": [],
            "blocks": [{"type": "body", "bbox": [100, 100, 900, 400], "text": f"Extracted {label}"}],
        }

    p1 = MockProvider(responder=lambda req: _wire_for(str(req.settings["page_number"])))
    res1 = run_extraction(
        "partial-book",
        kind="canonical",
        run_id="run-partial",
        pages="1-3",
        primary_provider_override=p1,
        repo_root=tmp_path,
        prefer_scan_checks=False,
    )
    assert len(res1["pages"]) == 3

    p2 = MockProvider(responder=lambda req: _wire_for(f"{req.settings['page_number']}-reextracted"))
    res2 = run_extraction(
        "partial-book",
        kind="canonical",
        run_id="run-partial",
        pages="2",
        resume=False,
        primary_provider_override=p2,
        repo_root=tmp_path,
        prefer_scan_checks=False,
    )
    assert [p["page_number"] for p in res2["pages"]] == [1, 2, 3]
    ir_pages = json.loads((repo.run_dir("partial-book", "run-partial", kind="canonical") / "ir" / "pages.json").read_text())
    assert [p["page_number"] for p in ir_pages["pages"]] == [1, 2, 3]
    assert ir_pages["pages"][0]["blocks"][0]["plain_text"] == "Extracted 1"
    assert ir_pages["pages"][1]["blocks"][0]["plain_text"] == "Extracted 2-reextracted"
    assert ir_pages["pages"][2]["blocks"][0]["plain_text"] == "Extracted 3"


def test_c3_worker_relay_requires_lease_token_and_preserves_p1_edits_across_p2(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None, "node runtime is required to test Cloudflare Worker script"

    worker_uri = (REPO_ROOT / "workers" / "proofing_relay" / "worker.js").as_uri()
    harness = tmp_path / "test_worker.mjs"
    sha_a = "a" * 64
    sha_b = "b" * 64
    harness.write_text(
        f"""
import assert from "node:assert/strict";
import crypto from "node:crypto";
import worker from {json.dumps(worker_uri)};

function sha256(s) {{
  return crypto.createHash("sha256").update(s).digest("hex");
}}

class FakeDB {{
  constructor() {{
    this.accounts = new Map([
      ["alice", {{ account_id: "alice", role: "proofreader", suspended: 0 }}],
      ["bob", {{ account_id: "bob", role: "trusted", suspended: 0 }}],
    ]);
    this.sessions = new Map([
      [sha256("tok-alice"), {{ account_id: "alice", expires_at: Date.now() + 600000 }}],
      [sha256("tok-bob"), {{ account_id: "bob", expires_at: Date.now() + 600000 }}],
    ]);
    this.leases = new Map();
    this.pageStates = new Map();
    this.events = [];
    this.counters = new Map();
  }}
  prepare(sql) {{
    const db = this;
    return {{
      args: [],
      bind(...args) {{ this.args = args; return this; }},
      async first() {{
        if (sql.includes("FROM sessions s JOIN accounts a")) {{
          const [h, now] = this.args;
          const sess = db.sessions.get(h);
          if (!sess || sess.expires_at <= now) return null;
          const acct = db.accounts.get(sess.account_id);
          return {{ account_id: acct.account_id, role: acct.role, suspended: acct.suspended }};
        }}
        if (sql.includes("FROM daily_counters")) {{
          return {{ count_value: db.counters.get(`${{this.args[0]}}:${{this.args[1]}}`) || 0 }};
        }}
        if (sql.includes("FROM page_leases")) {{
          return db.leases.get(`${{this.args[0]}}:${{this.args[1]}}`) || null;
        }}
        if (sql.includes("FROM page_states WHERE book_id")) {{
          return db.pageStates.get(`${{this.args[0]}}:${{this.args[1]}}`) || null;
        }}
        return null;
      }},
      async all() {{
        if (sql.includes("FROM page_states WHERE version > exported_version")) {{
          const results = [...db.pageStates.values()].filter((r) => r.version > r.exported_version);
          return {{ results }};
        }}
        return {{ results: [] }};
      }},
      async run() {{
        if (sql.includes("INSERT INTO daily_counters")) {{
          const k = `${{this.args[0]}}:${{this.args[1]}}`;
          db.counters.set(k, (db.counters.get(k) || 0) + 1);
        }} else if (sql.includes("INSERT INTO page_leases")) {{
          const [book_id, page_number, account_id, lease_token, claimed_at, expires_at] = this.args;
          db.leases.set(`${{book_id}}:${{page_number}}`, {{ book_id, page_number, account_id, lease_token, claimed_at, expires_at }});
        }} else if (sql.includes("INSERT INTO page_states")) {{
          const [book_id, page_number, status, version, source_page_sha256, page_text_sha256, p1_reviewer_id, p2_verifier_id, proposal_json, p1_proposal_json, updated_at] = this.args;
          const prev = db.pageStates.get(`${{book_id}}:${{page_number}}`);
          db.pageStates.set(`${{book_id}}:${{page_number}}`, {{
            book_id, page_number, status, version, source_page_sha256, page_text_sha256,
            p1_reviewer_id, p2_verifier_id, proposal_json, p1_proposal_json, updated_at,
            exported_version: prev ? prev.exported_version : 0,
          }});
        }} else if (sql.includes("DELETE FROM page_leases")) {{
          db.leases.delete(`${{this.args[0]}}:${{this.args[1]}}`);
        }} else if (sql.includes("INSERT INTO proof_events")) {{
          db.events.push(this.args);
        }}
        return {{ success: true }};
      }},
    }};
  }}
  async batch(stmts) {{
    for (const s of stmts) await s.run();
  }}
}}

const env = {{ DB: new FakeDB(), SKIP_DWELL_CHECK: "1", EXPORT_BEARER_TOKEN: "export-secret-token-123456" }};

async function post(path, token, body) {{
  const req = new Request(`https://relay.example${{path}}`, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json", Authorization: `Bearer ${{token}}` }},
    body: JSON.stringify(body),
  }});
  const resp = await worker.fetch(req, env);
  return {{ status: resp.status, body: await resp.json() }};
}}

const claim1 = await post("/api/claim", "tok-alice", {{ book_id: "test-book", page_number: 1 }});
assert.equal(claim1.status, 200);

// Missing lease_token MUST be rejected (409)
const missingLease = await post("/api/proposal", "tok-alice", {{
  version: "correction-v2",
  book_id: "test-book",
  corrections: [],
  page_signoff: {{ page_number: 1, status: "p1_proofed", reviewer: "alice", source_page_sha256: "{sha_a}", page_text_sha256: "{sha_b}" }},
}});
assert.equal(missingLease.status, 409);

// Valid P1 save with correction
const p1Save = await post("/api/proposal", "tok-alice", {{
  version: "correction-v2",
  book_id: "test-book",
  lease_token: claim1.body.lease_token,
  base_version: 0,
  corrections: [{{ correction_id: "c1", page_number: 1, block_id: "p0001-b0001", op: "set_text", replacement_tagged_text: "Induſtry" }}],
  letter_map_patch: {{ force_split_before_block_ids: ["p0001-b0001"] }},
  page_signoff: {{ page_number: 1, status: "p1_proofed", reviewer: "alice", source_page_sha256: "{sha_a}", page_text_sha256: "{sha_b}" }},
}});
assert.equal(p1Save.status, 201);

// P2 approves BEFORE daily export with empty corrections=[]
const claim2 = await post("/api/claim", "tok-bob", {{ book_id: "test-book", page_number: 1 }});
assert.equal(claim2.status, 200);
const p2Save = await post("/api/proposal", "tok-bob", {{
  version: "correction-v2",
  book_id: "test-book",
  lease_token: claim2.body.lease_token,
  base_version: 1,
  corrections: [],
  page_signoff: {{ page_number: 1, status: "verified", reviewer: "bob", source_page_sha256: "{sha_a}", page_text_sha256: "{sha_b}" }},
}});
assert.equal(p2Save.status, 201);
assert.equal(p2Save.body.status, "verified");

const expReq = new Request("https://relay.example/api/export", {{
  headers: {{ Authorization: "Bearer export-secret-token-123456" }},
}});
const expRes = await worker.fetch(expReq, env);
const expJson = await expRes.json();
assert.equal(expJson.items.length, 1);
assert.equal(expJson.items[0].proposal.corrections.length, 1);
assert.equal(expJson.items[0].proposal.corrections[0].correction_id, "c1");
assert.deepEqual(expJson.items[0].proposal.letter_map_patch.force_split_before_block_ids, ["p0001-b0001"]);
""",
        encoding="utf-8",
    )
    proc = subprocess.run([node_bin, str(harness)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Worker relay test failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"


def test_h1_proofing_studio_no_machine_p1_leak_csp_and_client_controls(tmp_path: Path) -> None:
    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "letter_header", "I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0002", 1, "body", "Text"),
            ],
            printed_folio="1",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "body", "Human edited text."),
            ],
            printed_folio="2",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="proof-h1", title="Proof H1")
    ledger = {
        "version": "proof-ledger-v2",
        "book_id": "proof-h1",
        "pages": {
            "1": {"page_number": 1, "status": "machine_reread", "signoff_method": "scan_check", "reviewer": "scan-agent"},
            "2": {"page_number": 2, "status": "p1_proofed", "signoff_method": "human", "p1_reviewer_id": "alice_human"},
        },
    }
    ed_dir = tmp_path / "ed"
    render_proofing_studio(ast, ed_dir, proof_ledger=ledger, relay_url="https://relay.example.org/api")

    s1 = json.loads((ed_dir / "proofing" / "pages" / "p0001.json").read_text(encoding="utf-8"))
    s2 = json.loads((ed_dir / "proofing" / "pages" / "p0002.json").read_text(encoding="utf-8"))
    assert s1["p1_reviewer"] is None
    assert s2["p1_reviewer"] == "alice_human"

    csp = _build_proofing_csp("https://relay.example.org/api")
    assert "https:;" not in csp
    assert "https://relay.example.org" in csp

    proof_html = (ed_dir / "proofing" / "index.html").read_text(encoding="utf-8")
    proof_js = (ed_dir / "proofing" / "app.js").read_text(encoding="utf-8")
    assert 'id="reviewer-token"' in proof_html
    assert 'id="btn-claim-page"' in proof_html
    assert "note-label-input" in proof_js
    assert "/api/claim" in proof_js
    assert "Authorization" in proof_js
    assert "state.forceSplits = {};" in proof_js
    assert "state.forbidSplits = {};" in proof_js
    assert "WebCrypto SHA-256 unavailable" in proof_js


def test_h11_cli_compile_renders_once_and_fails_nonzero_on_gate_error(tmp_path: Path, monkeypatch) -> None:
    repo = RepoPaths.from_root(tmp_path)
    BookConfig(book_id="cli-compile-book", title="CLI Compile Test", active_run_id="run-01").save(paths=repo)
    run_dir = repo.run_dir("cli-compile-book", "run-01", kind="canonical")
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
    (run_dir / "ir").mkdir(parents=True)
    (run_dir / "ir" / "pages.json").write_text(
        json.dumps({"pages": [p.model_dump(mode="json") for p in pages]}),
        encoding="utf-8",
    )

    render_calls = 0
    real_render = render_complete_edition

    def _counting_render(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal render_calls
        render_calls += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr("lanterns.cli.render_complete_edition", _counting_render)
    args = argparse.Namespace(
        book_id="cli-compile-book",
        run_id=None,
        set_active=False,
        bundle_scans=False,
        output_dir=None,
        publish_preview_to_library=False,
        relay_url="",
        source_pdf=None,
    )
    assert _cmd_compile(args, repo) == 0
    assert render_calls == 1

    monkeypatch.setattr(
        "lanterns.validation.validate_compiled_edition",
        lambda *_a, **_kw: {"ok": False, "valid": False, "error_count": 1, "errors": [{"surface": "epub", "code": "err", "message": "boom"}], "findings": []},
    )
    assert _cmd_compile(args, repo) == 1


def test_h10_no_argon_or_disqualified_qwen38_flash_in_profiles() -> None:
    profiles = load_profiles(str(REPO_ROOT))
    routes: list[str] = []
    for pname, pdata in profiles.items():
        for key in ("primary", "hotspot_model", "boundary_model", "decision_model"):
            if pdata.get(key):
                routes.append(str(pdata[key]))
        routes.extend(str(r) for r in (pdata.get("fallback_chain") or []) if r)
        assert "gemini-4-argon" not in json.dumps(pdata), f"argon remains in profile {pname}"
        assert "qwen3.8-flash" not in json.dumps(pdata), f"qwen3.8-flash remains in profile {pname}"
    ensure_priced_routes(routes, root=str(REPO_ROOT))
    hv_src = (REPO_ROOT / "src" / "lanterns" / "extraction" / "hotspot_verifier.py").read_text(encoding="utf-8")
    assert "gemini-4-argon" not in hv_src


def test_h9_archive_org_raw_leaf_resolution_slice_and_book_toml(tmp_path: Path) -> None:
    assert (
        build_iiif_page_url("test_id", 5, use_iiif_endpoint=True, raw_leaf_number=9)
        == "https://iiif.archive.org/iiif/test_id$9/full/max/0/default.jpg"
    )
    assert (
        build_iiif_page_url("test_id", 5, width=1200)
        == "https://archive.org/download/test_id/page/n5_w1200.jpg"
    )
    assert build_iiif_info_url("test_id", 9) == "https://iiif.archive.org/iiif/test_id$9/info.json"

    # Mid-book missing XML/JP2 leaf count mismatch still aligns 0-based scandata when max matches offset
    jp2s = ["b_0001.jp2", "b_0002.jp2", "b_0004.jp2", "b_0005.jp2"]
    scandata = """
    <bookData><pageData>
      <page leafNum="0"><addToAccessFormats>false</addToAccessFormats></page>
      <page leafNum="1"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="2"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="3"><addToAccessFormats>false</addToAccessFormats></page>
      <page leafNum="4"><addToAccessFormats>true</addToAccessFormats></page>
    </pageData></bookData>
    """
    assert access_leaves(jp2s, scandata, expected_page_count=None) == ["b_0002.jp2", "b_0005.jp2"]
    assert resolve_raw_leaf_numbers(jp2s, scandata) == [2, 5]

    # Slice extraction uses accessible index only (never matching pos OR raw leaf_no twice)
    jp2_zip = tmp_path / "item_jp2.zip"
    with zipfile.ZipFile(jp2_zip, "w") as zf:
        for name in jp2s:
            buf = io.BytesIO()
            Image.new("L", (40, 40), color=240).save(buf, format="PNG")
            zf.writestr(f"item_jp2/{name}", buf.getvalue())
    extracted = _extract_jp2_slice_if_requested(jp2_zip, tmp_path / "out", [1], scandata_xml=scandata)
    assert [Path(p).name for p in extracted] == ["b_0005.jp2"]

    nap = BookConfig.load("napoleon-joseph-vol1", paths=RepoPaths.from_root(REPO_ROOT))
    goe = BookConfig.load("goethe-zelter", paths=RepoPaths.from_root(REPO_ROOT))
    assert nap.archive_id == "confidentialcorre01napouoft"
    assert len(nap.source_pdf_sha256) == 64
    assert goe.archive_id == "goetheslettersto00goetrich"
    assert len(goe.source_pdf_sha256) == 64


def test_h2_scan_overrides_audit_jsonl_and_letter_map_merge(tmp_path: Path) -> None:
    real_repo = RepoPaths.from_root(REPO_ROOT)
    assert list(real_repo.books_dir.glob("*/scan_checks")) == []
    override_13 = load_native_scan_check(real_repo.book_dir("newton-bentley-1756"), 13)
    assert override_13 is not None
    assert override_13.data["blocks"] and "ſtrong" in override_13.data["blocks"][1]["text"]
    assert real_repo.audit_checks_path("bapu-mira-letters").read_text(encoding="utf-8").strip()

    # Legacy `scan_checks/` containing audit `"checks"` payload returns None instead of crashing
    repo = RepoPaths.from_root(tmp_path)
    bdir = repo.book_dir("demo-book")
    legacy_dir = repo.scan_checks_dir("demo-book")
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "page_0001.json").write_text(
        json.dumps({"book_id": "demo-book", "page_number": 1, "check_id": "audit-1", "checks": []}),
        encoding="utf-8",
    )
    assert load_native_scan_check(bdir, 1) is None

    # Intake writes wire/blocks overrides to `scan_overrides/` and `"checks"` to `audit_checks.jsonl`
    prop = tmp_path / "scan_check_p2.json"
    prop.write_text(
        json.dumps(
            {
                "book_id": "demo-book",
                "page_number": 2,
                "check_id": "sc-2",
                "reviewer": "qa-tester",
                "blocks": [{"type": "body", "bbox": [100, 100, 900, 300], "text": "Confirmed archival text."}],
                "checks": [{"block_id": "p0002-b0001", "verdict": "printed_as_read"}],
            }
        ),
        encoding="utf-8",
    )
    res = intake_proposal("demo-book", prop, repo_root=tmp_path)
    assert res["kind"] == "scan_check"
    assert scan_override_path(bdir, 2).is_file()
    loaded_2 = load_native_scan_check(bdir, 2)
    assert loaded_2 is not None and loaded_2.data["blocks"][0]["text"] == "Confirmed archival text."
    audit_lines = repo.audit_checks_path("demo-book").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["check_id"] == "sc-2"

    # `_merge_letter_map_patch` deduplicates `explicit_letters` and preserves `enclosure_block_ranges`
    patch = {
        "explicit_letters": [{"letter_id": "L-001", "start_block_id": "p0002-b0001", "end_block_id": "p0003-b0002"}],
        "enclosure_block_ranges": [{"start_block_id": "p0002-b0004", "end_block_id": "p0002-b0006", "kind": "enclosure"}],
    }
    _merge_letter_map_patch("demo-book", patch, tmp_path)
    _merge_letter_map_patch("demo-book", patch, tmp_path)
    lmap = load_letter_map("demo-book", repo_root=tmp_path)
    assert len(lmap["explicit_letters"]) == 1
    assert len(lmap["enclosure_block_ranges"]) == 1


def test_h4_ornamental_break_before_closer_does_not_split_letter() -> None:
    p1 = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "letter_header", "I.", epistolary_transition="starts_new_letter"),
            make_block("p0001-b0002", 1, "salutation", "My dear Sir,"),
            make_block("p0001-b0003", 1, "body", "Pray accept my warmest thanks for your kind letter."),
            make_block("p0001-b0004", 1, "ornamental_break", "* * *"),
            make_block("p0001-b0005", 1, "valediction", "Yours faithfully,"),
            make_block("p0001-b0006", 1, "signature", "J. R."),
        ],
        printed_folio="5",
    )
    ast = compile_collection_ast([p1], book_id="ornament-book")
    assert len(ast.letters) == 1
    assert ast.letters[0].block_ids == [b.block_id for b in p1.blocks]


def test_m1_monotonic_folios_respects_last_arabic_and_blank_pages() -> None:
    pages = [
        make_page(1, [make_block("p0001-b0001", 1, "body", "Page ten.")], printed_folio="10"),
        make_page(2, [make_block("p0002-b0001", 2, "body", "Page eleven without running header.")]).model_copy(
            update={"printed_folio": None}
        ),
        make_page(3, [make_block("p0003-b0001", 3, "body", "Page twelve.")], printed_folio="12"),
        make_page(4, [make_block("p0004-b0001", 4, "body", "Page thirteen.")], printed_folio="13"),
        make_page(5, [make_block("p0005-b0001", 5, "body", "Blank versos.")], warning_codes=["blank_page"]).model_copy(
            update={"printed_folio": None}
        ),
        make_page(6, []).model_copy(update={"printed_folio": None}),
    ]
    solved = solve_monotonic_folios(pages)
    assert solved[1].printed_folio == "11"
    assert solved[4].printed_folio is None
    assert solved[5].printed_folio is None


def test_m2_unicode_cross_page_joins_fraktur_hyphen_and_index_wraps() -> None:
    assert {"index_entry", "toc_entry"} <= DEHYPHENATABLE_PROSE_TYPES
    p1 = make_page(
        1,
        [make_block("p0001-b0001", 1, "index_entry", "Freund⸗\nschaft und Briefwechsel, 14.")],
        printed_folio="200",
    )
    wrapped, _ = resolve_page_ir_line_wraps([p1])
    assert "Freundschaft" in wrapped[0].blocks[0].plain_text

    # Cross-page IAST, Greek, and Cyrillic word joins
    counts = Counter({"mahākṛṣṇa": 3, "φιλοσοφία": 3, "письмо": 3})
    l_sa, r_sa = dedup_or_join_cross_page_tagged("spoke of mahākṛ-", "ṣṇa today.", counts, Counter())
    assert "mahākṛṣṇa" in l_sa and r_sa == "today."
    l_el, r_el = dedup_or_join_cross_page_tagged("περὶ φιλοσο-", "φίας.", counts, Counter())
    assert l_el == "περὶ φιλοσοφίας" and r_el == "."
    l_ru, r_ru = dedup_or_join_cross_page_tagged("читал пись-", "мо.", counts, Counter())
    assert l_ru == "читал письмо" and r_ru == "."


def test_m3_bare_symbol_promotion_guards_ciphers_and_satisfied_footnotes() -> None:
    for cipher in ("saw Mr. B* at court", "wrote to B*— yesterday", "told Count *** everything"):
        out, changed = promote_bare_symbol_in_tagged(cipher, "*")
        assert changed is False and out == cipher

    promo, hit = promote_bare_symbol_in_tagged("Chiapé* wrote yesterday.", "*")
    assert hit is True and promo == "Chiapé<sup>*</sup> wrote yesterday."

    # Page already has `<sup>*</sup>` satisfying its `*` footnote -> leave cipher `B*` alone
    page = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "body", "Chiapé<sup>*</sup> wrote to Mr. B* at night."),
            make_block("p0001-b0002", 1, "footnote", "<sup>*</sup> Note text.", note_label="*"),
        ],
        printed_folio="7",
    )
    out_pages = _promote_page_symbol_callouts([page])
    assert out_pages[0].blocks[0].tagged_text == "Chiapé<sup>*</sup> wrote to Mr. B* at night."


def test_m4_validators_furniture_non_body_unsilenced_anomalies_and_case_diacritics() -> None:
    # Non-body furniture leaks (trailing folio on signature; repeated top heading across 3 pages)
    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "heading", "LETTERS OF ERASMUS"),
                make_block("p0001-b0002", 1, "body", "Letter body."),
                make_block("p0001-b0003", 1, "signature", "1", bbox=[450, 920, 550, 950]),
            ],
            printed_folio="1",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "heading", "LETTERS OF ERASMUS"),
                make_block("p0002-b0002", 2, "body", "Second letter body."),
            ],
            printed_folio="2",
        ),
        make_page(
            3,
            [
                make_block("p0003-b0001", 3, "editorial", "LETTERS OF ERASMUS"),
                make_block("p0003-b0002", 3, "body", "Third page."),
            ],
            printed_folio="3",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="m4-book")
    fl_findings = furniture_leak(ast)
    fl_blocks = {f.block_id for f in fl_findings}
    assert "p0001-b0003" in fl_blocks  # folio numeral inside signature
    assert {"p0001-b0001", "p0002-b0001", "p0003-b0001"} <= fl_blocks

    # `human_mapped` must NOT silence multiple_signatures_without_enclosure (only `adjudicated` or confirmed_block_ids)
    multi_sig_page = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "letter_header", "I.", epistolary_transition="starts_new_letter"),
            make_block("p0001-b0002", 1, "body", "Body one."),
            make_block("p0001-b0003", 1, "signature", "Alice."),
            make_block("p0001-b0004", 1, "signature", "Bob."),
        ],
        printed_folio="10",
    )
    ast_hm = compile_collection_ast(
        [multi_sig_page],
        book_id="sig-book",
        letter_map={"forbid_split_before_block_ids": ["p0001-b0004"]},
    )
    assert ast_hm.letters[0].boundary_confidence == "human_mapped"
    anom_hm = epistolary_boundary_anomalies(ast_hm)
    assert any(f.code == "multiple_signatures_without_enclosure" and f.severity == "warning" for f in anom_hm)
    ast_adj = ast_hm.model_copy(
        update={"letters": [ast_hm.letters[0].model_copy(update={"boundary_confidence": "adjudicated"})]}
    )
    anom_adj = epistolary_boundary_anomalies(ast_adj)
    assert all(f.severity == "info" for f in anom_adj if f.code == "multiple_signatures_without_enclosure")

    # Lowercase common word / all-caps must not cross-trigger capitalized proper noun diacritic drift
    diac_page = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "body", "Il traite le Traité ici. Voir TRAITE et Traité, Traité, Traité."),
            make_block("p0001-b0002", 1, "body", "Encore un Traite sans accent."),
        ],
        printed_folio="12",
    )
    ast_d = compile_collection_ast([diac_page], book_id="diac-book")
    diac_warn = [
        f
        for f in diacritic_and_script_integrity(ast_d)
        if f.code == "diacritic_drift_advisory" and f.severity == "warning"
    ]
    assert len(diac_warn) == 1
    assert diac_warn[0].evidence["bare_form"] == "Traite"


def test_m5_epistolary_exam_non_vacuous_and_folio_grading(tmp_path: Path) -> None:
    manifest_res = evaluate_epistolary_exam(REPO_ROOT)
    assert manifest_res["ok"] is True
    assert manifest_res["vacuous"] is True
    assert manifest_res["mode"] == "manifest_only"

    strict_res = evaluate_epistolary_exam(REPO_ROOT, require_predictions=True)
    assert strict_res["ok"] is False

    # Synthetic 2-case exam catching folio mismatch & missing predictions
    edir = tmp_path / "exam"
    (edir / "expectations").mkdir(parents=True)
    pdir = tmp_path / "preds"
    pdir.mkdir(parents=True)
    (edir / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {"case_id": "t1", "split": "tuning", "expectation_file": "expectations/t1.json"},
                    {"case_id": "h1", "split": "held_out", "expectation_file": "expectations/h1.json"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (edir / "expectations" / "t1.json").write_text(
        json.dumps({"acceptable_printed_folios": ["20", None], "must_contain_exact": ["Hello"]}),
        encoding="utf-8",
    )
    (edir / "expectations" / "h1.json").write_text(
        json.dumps({"expected_printed_folio": "21", "must_contain_exact": ["World"]}),
        encoding="utf-8",
    )
    p_bad = make_page(45, [make_block("p0045-b0001", 45, "body", "Hello")], printed_folio="[30]")
    (pdir / "t1.json").write_text(p_bad.model_dump_json(), encoding="utf-8")

    scored = evaluate_epistolary_exam(tmp_path, exam_dir=edir, predictions_dir=pdir, require_predictions=True)
    assert scored["ok"] is False
    assert scored["mode"] == "predictions"
    msgs = {f["case_id"]: f["message"] for f in scored["failures"]}
    assert "printed_folio" in msgs["t1"]
    assert "missing prediction" in msgs["h1"]

    repo = RepoPaths.from_root(REPO_ROOT)
    ns_fail = argparse.Namespace(exam_dir=None, predictions_dir=None, validate_manifest_only=False)
    assert _cmd_benchmark(ns_fail, repo) == 1
    ns_ok = argparse.Namespace(exam_dir=None, predictions_dir=None, validate_manifest_only=True)
    assert _cmd_benchmark(ns_ok, repo) == 0

