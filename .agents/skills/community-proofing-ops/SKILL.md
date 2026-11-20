---
name: community-proofing-ops
description: >-
  Use this skill when building or testing the zero-innerHTML Proofing Studio, ingesting
  Distributed Proofreaders (P1 -> P2) volunteer proposal JSON files via lanterns intake,
  verifying sacred human-text protection, or operating the Cloudflare Worker/D1 relay.
---

# Community Proofing (`P1 -> P2`) Operations Workflow

## Step 1 — Build & Security-Check the Static Proofing Studio

1. Generate or refresh the book's Proofing Studio (`library/<collection>/<book_id>/proofing/`):
   ```bash
   uv run lanterns proof-site <book_id>
   ```
   *(Pass `--bundle-scans` during `compile` when local offline scan PNGs are needed alongside Internet Archive IIIF streaming.)*
2. Verify security invariants via `uv run lanterns doctor` and `uv run lanterns validate-edition <book_id>`:
   - Zero occurrences of `innerHTML`, `outerHTML`, or `insertAdjacentHTML`.
   - Strict `<meta http-equiv="Content-Security-Policy">` present in `proofing/index.html`.
   - Per-page JSON shards present in `proofing/pages/pNNNN.json`.

## Step 2 — Ingest Community Proposals (`P1 -> P2` Governance)

1. Validate incoming proposal files against `schemas/correction_v2.schema.json` and ingest via CLI:
   ```bash
   uv run lanterns intake <book_id> path/to/proposal.json
   ```
2. Verify the three `P1 -> P2` invariants in `books/<book_id>/proof_ledger.json`:
   - Round 1 human save transitions `unproofed` / `machine_reread` -> `p1_proofed` (`p1_reviewer_id` recorded).
   - Round 2 confirmation transitions `p1_proofed` -> `verified` **only** when `p2_verifier_id != p1_reviewer_id` and zero text edits were needed (if Round 2 edits text, reviewer becomes new `p1_reviewer_id`).
   - Subsequent AI extraction re-runs **never** overwrite blocks on `p1_proofed` or `verified` pages.

## Step 3 — Recompile Edition at `$0` API Spend

1. Re-run `compile` and `status` to replay human corrections into Web, Standalone Letters, EPUB 3, Markdown, and TEI P5 XML without calling any VLM:
   ```bash
   uv run lanterns compile <book_id> --set-active
   uv run lanterns validate-edition <book_id>
   uv run lanterns status
   ```
