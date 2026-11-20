"""Phase 0 regression tests covering security (C1), data integrity (C2/C3), proofing studio (H1), and CLI compile (H11)."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pymupdf as fitz

from lanterns.cli import _cmd_compile
from lanterns.core.config import BookConfig
from lanterns.core.paths import RepoPaths
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.extraction.pipeline import run_extraction
from lanterns.providers.base import MockProvider
from lanterns.renderers import render_complete_edition
from lanterns.renderers.html_utils import _format_folio_html
from lanterns.renderers.proofing_studio import _build_proofing_csp, render_proofing_studio
from lanterns.renderers.render_book_web import READER_JS
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
