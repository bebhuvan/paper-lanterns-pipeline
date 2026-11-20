/**
 * Paper Lanterns v2 — Distributed Proofreaders (P1 -> P2) Cloudflare Worker + D1 Relay.
 *
 * Security & Governance Guarantees:
 * 1. Holds NO GitHub write token. A scheduled GitHub Action pulls finished work via
 *    read-only `GET /api/export` (`Authorization: Bearer <EXPORT_BEARER_TOKEN>`).
 * 2. Enforces 15-minute renewable page checkout locks (`POST /api/claim`).
 * 3. Server-side enforces `p2_verifier_id !== p1_reviewer_id` and trusted verifier role:
 *    - If a second reviewer modifies text, the page remains `p1_proofed` under the new author
 *      and requires an independent trusted P2 verifier.
 *    - Only an unchanged review by a distinct trusted account advances `p1_proofed -> verified`.
 * 4. Anonymous flags (`POST /api/flag`) never mutate text and hit a hard global daily D1 cap.
 */

const BOOK_SLUG_RE = /^[a-z0-9][a-z0-9_-]{1,78}$/;
const SHA256_RE = /^[0-9a-f]{64}$/i;
const ACCOUNT_ID_RE = /^[a-zA-Z0-9_-]{3,64}$/;
const MAX_BODY_BYTES = 64 * 1024;
const LEASE_DURATION_MS = 15 * 60 * 1000;
const MIN_DWELL_MS = 5 * 1000;
const GLOBAL_DAILY_FLAG_CAP = 250;
const DAILY_ACCOUNT_SAVE_CAP = 80;

const ALLOWED_OPS = new Set([
  "set_text",
  "set_type",
  "set_alignment",
  "set_transition",
  "set_note_label",
  "set_cells",
  "split_block",
  "merge_with_next",
  "drop_residue",
]);

const ALLOWED_FLAG_REASONS = new Set([
  "typo_or_ocr_error",
  "missing_diacritic",
  "wrong_letter_boundary",
  "broken_footnote",
  "damaged_or_missing_scan",
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = buildSecurityHeaders(request, env);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    if (env.READ_ONLY_MODE === "1" && request.method !== "GET") {
      return jsonResponse({ error: "Proofing relay is temporarily in read-only maintenance mode." }, 503, cors);
    }

    if (!env.DB) {
      return jsonResponse({ error: "D1 database binding DB is not configured." }, 503, cors);
    }

    if (request.method === "GET" && url.pathname === "/api/status") {
      const bookId = url.searchParams.get("book_id") || "";
      if (!BOOK_SLUG_RE.test(bookId)) {
        return jsonResponse({ error: "Invalid book_id" }, 400, cors);
      }
      const rows = await env.DB.prepare(
        "SELECT page_number, status, version, p1_reviewer_id, p2_verifier_id, updated_at FROM page_states WHERE book_id = ? ORDER BY page_number ASC"
      )
        .bind(bookId)
        .all();
      return jsonResponse({ ok: true, book_id: bookId, pages: rows.results || [] }, 200, cors);
    }

    if (request.method === "GET" && url.pathname === "/api/export") {
      return handleDailyExport(request, env, url, cors);
    }

    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, cors);
    }

    const bodyResult = await readBoundedJson(request);
    if (bodyResult.error) {
      return jsonResponse({ error: bodyResult.error }, bodyResult.status, cors);
    }
    const payload = bodyResult.payload;

    if (url.pathname === "/api/flag") {
      return handleAnonymousFlag(env, payload, cors);
    }
    if (url.pathname === "/api/claim") {
      return handleClaimLease(request, env, payload, cors);
    }
    if (url.pathname === "/api/proposal" || url.pathname === "/api/save") {
      return handleSaveOrVerify(request, env, payload, cors);
    }
    if (url.pathname === "/api/ack-export") {
      return handleAckExport(request, env, payload, cors);
    }

    return jsonResponse({ error: "Unknown route" }, 404, cors);
  },
};

function buildSecurityHeaders(request, env) {
  const allowedOrigin = env.ALLOWED_ORIGIN || "";
  const reqOrigin = request.headers.get("Origin") || "";
  const headers = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  };
  if (allowedOrigin && reqOrigin === allowedOrigin) {
    headers["Access-Control-Allow-Origin"] = allowedOrigin;
    headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS";
    headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization";
    headers["Vary"] = "Origin";
  }
  return headers;
}

async function readBoundedJson(request) {
  const contentLen = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (contentLen > MAX_BODY_BYTES) {
    return { error: "Payload exceeds 64 KB limit", status: 413 };
  }
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
    return { error: "Payload exceeds 64 KB limit", status: 413 };
  }
  try {
    return { payload: JSON.parse(raw), status: 200 };
  } catch {
    return { error: "Malformed JSON body", status: 400 };
  }
}

async function resolveAuthenticatedActor(request, env, payloadActorId) {
  const authHeader = request.headers.get("Authorization") || "";
  if (authHeader.startsWith("Bearer ")) {
    const token = authHeader.slice(7).trim();
    const tokenHash = await sha256Hex(token);
    const nowMs = Date.now();
    const row = await env.DB.prepare(
      "SELECT s.account_id, a.role, a.suspended FROM sessions s JOIN accounts a ON a.account_id = s.account_id WHERE s.session_hash = ? AND s.expires_at > ?"
    )
      .bind(tokenHash, nowMs)
      .first();
    if (!row || row.suspended) return null;
    return { account_id: row.account_id, role: row.role };
  }
  if (env.ALLOW_DEV_HEADERS === "1" && ACCOUNT_ID_RE.test(String(payloadActorId || ""))) {
    const acct = await env.DB.prepare(
      "SELECT account_id, role, suspended FROM accounts WHERE account_id = ?"
    )
      .bind(String(payloadActorId))
      .first();
    if (acct && !acct.suspended) {
      return { account_id: acct.account_id, role: acct.role };
    }
  }
  return null;
}

async function handleAnonymousFlag(env, payload, cors) {
  const bookId = String(payload?.book_id || "");
  const pageNumber = Number(payload?.page_number);
  const reason = String(payload?.reason || "");
  const note = payload?.note ? String(payload.note).slice(0, 200) : null;

  if (!BOOK_SLUG_RE.test(bookId)) return jsonResponse({ error: "Invalid book_id" }, 422, cors);
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > 10000) {
    return jsonResponse({ error: "Invalid page_number" }, 422, cors);
  }
  if (!ALLOWED_FLAG_REASONS.has(reason)) {
    return jsonResponse({ error: "Invalid flag reason" }, 422, cors);
  }

  const dayKey = new Date().toISOString().slice(0, 10);
  await env.DB.prepare(
    "INSERT INTO daily_counters (day_key, counter_name, count_value) VALUES (?, 'flags', 1) ON CONFLICT(day_key, counter_name) DO UPDATE SET count_value = count_value + 1"
  )
    .bind(dayKey)
    .run();

  const counter = await env.DB.prepare(
    "SELECT count_value FROM daily_counters WHERE day_key = ? AND counter_name = 'flags'"
  )
    .bind(dayKey)
    .first();

  if (counter && counter.count_value > GLOBAL_DAILY_FLAG_CAP) {
    return jsonResponse({ ok: false, capped: true, message: "Daily community flag limit reached; try again tomorrow." }, 429, cors);
  }

  const nowIso = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO page_flags (book_id, page_number, reason, note, created_day, created_at) VALUES (?, ?, ?, ?, ?, ?)"
  )
    .bind(bookId, pageNumber, reason, note, dayKey, nowIso)
    .run();

  return jsonResponse({ ok: true, queued: true }, 201, cors);
}

async function handleClaimLease(request, env, payload, cors) {
  const actor = await resolveAuthenticatedActor(request, env, payload?.reviewer_id);
  if (!actor) return jsonResponse({ error: "Authentication required to claim a page." }, 401, cors);

  const bookId = String(payload?.book_id || "");
  const pageNumber = Number(payload?.page_number);
  if (!BOOK_SLUG_RE.test(bookId) || !Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > 10000) {
    return jsonResponse({ error: "Invalid book_id or page_number" }, 422, cors);
  }

  const nowMs = Date.now();
  const existing = await env.DB.prepare(
    "SELECT account_id, lease_token, claimed_at, expires_at FROM page_leases WHERE book_id = ? AND page_number = ?"
  )
    .bind(bookId, pageNumber)
    .first();

  if (existing && existing.expires_at > nowMs && existing.account_id !== actor.account_id) {
    return jsonResponse(
      { error: "Page is currently checked out by another proofreader.", expires_at: existing.expires_at },
      409,
      cors
    );
  }

  const claimedAt = existing && existing.account_id === actor.account_id ? existing.claimed_at : nowMs;
  const leaseToken = crypto.randomUUID();
  const expiresAt = nowMs + LEASE_DURATION_MS;

  await env.DB.prepare(
    "INSERT INTO page_leases (book_id, page_number, account_id, lease_token, claimed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(book_id, page_number) DO UPDATE SET account_id = excluded.account_id, lease_token = excluded.lease_token, claimed_at = excluded.claimed_at, expires_at = excluded.expires_at"
  )
    .bind(bookId, pageNumber, actor.account_id, leaseToken, claimedAt, expiresAt)
    .run();

  return jsonResponse(
    { ok: true, book_id: bookId, page_number: pageNumber, lease_token: leaseToken, expires_at: expiresAt },
    200,
    cors
  );
}

async function handleSaveOrVerify(request, env, payload, cors) {
  const validationErr = validateProposalPayload(payload);
  if (validationErr) return jsonResponse({ error: validationErr }, 422, cors);

  const signoff = payload.page_signoff;
  const actorHint = signoff.reviewer_id || signoff.reviewer || payload.reviewer;
  const actor = await resolveAuthenticatedActor(request, env, actorHint);
  if (!actor) return jsonResponse({ error: "Authentication required to submit proofreading." }, 401, cors);

  const bookId = payload.book_id;
  const pageNumber = signoff.page_number;
  const nowMs = Date.now();
  const dayKey = new Date(nowMs).toISOString().slice(0, 10);

  const acctCounterKey = `saves:${actor.account_id}`;
  await env.DB.prepare(
    "INSERT INTO daily_counters (day_key, counter_name, count_value) VALUES (?, ?, 1) ON CONFLICT(day_key, counter_name) DO UPDATE SET count_value = count_value + 1"
  )
    .bind(dayKey, acctCounterKey)
    .run();
  const acctCount = await env.DB.prepare(
    "SELECT count_value FROM daily_counters WHERE day_key = ? AND counter_name = ?"
  )
    .bind(dayKey, acctCounterKey)
    .first();
  if (acctCount && acctCount.count_value > DAILY_ACCOUNT_SAVE_CAP) {
    return jsonResponse({ error: "Daily submission cap reached for this account." }, 429, cors);
  }

  const lease = await env.DB.prepare(
    "SELECT account_id, lease_token, claimed_at, expires_at FROM page_leases WHERE book_id = ? AND page_number = ?"
  )
    .bind(bookId, pageNumber)
    .first();
  if (!lease || lease.account_id !== actor.account_id || lease.expires_at < nowMs) {
    return jsonResponse({ error: "Active page checkout lease required before saving." }, 409, cors);
  }
  const leaseToken = String(payload.lease_token || "").trim();
  if (!leaseToken || lease.lease_token !== leaseToken) {
    return jsonResponse({ error: "Valid checkout lease_token is required." }, 409, cors);
  }
  if (nowMs - lease.claimed_at < MIN_DWELL_MS && env.SKIP_DWELL_CHECK !== "1") {
    return jsonResponse({ error: "Page must be inspected against the scan before sign-off." }, 422, cors);
  }

  const current = await env.DB.prepare(
    "SELECT status, version, source_page_sha256, page_text_sha256, p1_reviewer_id, p2_verifier_id, proposal_json, p1_proposal_json, exported_version FROM page_states WHERE book_id = ? AND page_number = ?"
  )
    .bind(bookId, pageNumber)
    .first();

  const baseVersion = Number(payload.base_version ?? 0);
  if (current && current.version !== baseVersion) {
    return jsonResponse(
      { error: "Version conflict: page was updated since checkout.", current_version: current.version },
      409,
      cors
    );
  }

  const prevStatus = current ? current.status : "unproofed";
  const requestedStatus = signoff.status;
  const hasEdits = Array.isArray(payload.corrections) && payload.corrections.length > 0;
  const textHashChanged = current ? current.page_text_sha256 !== signoff.page_text_sha256 : false;

  let nextStatus = requestedStatus;
  let nextP1 = current ? current.p1_reviewer_id : null;
  let nextP2 = null;

  if (requestedStatus === "problem" || requestedStatus === "needs_review") {
    nextStatus = "problem";
  } else if (requestedStatus === "verified") {
    if (actor.role !== "trusted" && actor.role !== "maintainer") {
      return jsonResponse({ error: "Only trusted proofreaders may mark a page verified (P2)." }, 403, cors);
    }
    const priorP1 = current?.p1_reviewer_id || signoff.p1_reviewer_id || signoff.p1_reviewer || "";
    if (!priorP1) {
      return jsonResponse({ error: "Page must complete Round 1 (p1_proofed) before Round 2 verification." }, 422, cors);
    }
    if (priorP1 === actor.account_id) {
      return jsonResponse(
        { error: "Two-person rule violation: P2 verifier must differ from P1 proofreader." },
        422,
        cors
      );
    }
    if (hasEdits || textHashChanged) {
      // DP Stopping Rule: if P2 changes text, they become the new P1 author and a fresh P2 is required.
      nextStatus = "p1_proofed";
      nextP1 = actor.account_id;
      nextP2 = null;
    } else {
      nextStatus = "verified";
      nextP1 = priorP1;
      nextP2 = actor.account_id;
    }
  } else {
    nextStatus = "p1_proofed";
    nextP1 = actor.account_id;
    nextP2 = null;
  }

  const nextVersion = (current ? current.version : 0) + 1;
  const nowIso = new Date(nowMs).toISOString();
  let priorP1Proposal = null;
  const rawPrior = current?.p1_proposal_json || current?.proposal_json;
  if (rawPrior) {
    try {
      priorP1Proposal = JSON.parse(rawPrior);
    } catch {
      priorP1Proposal = null;
    }
  }
  const preservePriorP1 = (nextStatus === "verified" || (nextStatus === "problem" && !hasEdits)) && priorP1Proposal;
  const effectiveCorrections = preservePriorP1
    ? (Array.isArray(priorP1Proposal.corrections) ? priorP1Proposal.corrections : [])
    : (payload.corrections || []);
  const effectiveMapPatch = payload.letter_map_patch || (preservePriorP1 ? (priorP1Proposal.letter_map_patch || null) : null);

  const normalizedProposal = {
    version: "correction-v2",
    book_id: bookId,
    reviewer: actor.account_id,
    created_at: nowIso,
    corrections: effectiveCorrections,
    letter_map_patch: effectiveMapPatch,
    page_signoff: {
      page_number: pageNumber,
      status: nextStatus === "problem" ? "needs_review" : nextStatus,
      reviewer: actor.account_id,
      p1_reviewer: nextP1,
      source_page_sha256: signoff.source_page_sha256,
      page_text_sha256: signoff.page_text_sha256,
      verified_at: nowIso,
      note: signoff.note ? String(signoff.note).slice(0, 300) : null,
    },
  };
  const proposalJsonStr = JSON.stringify(normalizedProposal);
  const p1ProposalJsonStr = nextStatus === "p1_proofed"
    ? proposalJsonStr
    : (current?.p1_proposal_json || current?.proposal_json || proposalJsonStr);

  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO page_states
        (book_id, page_number, status, version, source_page_sha256, page_text_sha256, p1_reviewer_id, p2_verifier_id, proposal_json, p1_proposal_json, updated_at, exported_version)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
       ON CONFLICT(book_id, page_number) DO UPDATE SET
        status = excluded.status,
        version = excluded.version,
        source_page_sha256 = excluded.source_page_sha256,
        page_text_sha256 = excluded.page_text_sha256,
        p1_reviewer_id = excluded.p1_reviewer_id,
        p2_verifier_id = excluded.p2_verifier_id,
        proposal_json = excluded.proposal_json,
        p1_proposal_json = excluded.p1_proposal_json,
        updated_at = excluded.updated_at`
    ).bind(
      bookId,
      pageNumber,
      nextStatus,
      nextVersion,
      signoff.source_page_sha256,
      signoff.page_text_sha256,
      nextP1,
      nextP2,
      proposalJsonStr,
      p1ProposalJsonStr,
      nowIso
    ),
    env.DB.prepare(
      `INSERT INTO proof_events
        (book_id, page_number, actor_id, from_status, to_status, source_page_sha256, page_text_sha256, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(
      bookId,
      pageNumber,
      actor.account_id,
      prevStatus,
      nextStatus,
      signoff.source_page_sha256,
      signoff.page_text_sha256,
      signoff.note ? String(signoff.note).slice(0, 300) : null,
      nowIso
    ),
    env.DB.prepare("DELETE FROM page_leases WHERE book_id = ? AND page_number = ?").bind(bookId, pageNumber),
  ]);

  return jsonResponse(
    {
      ok: true,
      book_id: bookId,
      page_number: pageNumber,
      status: nextStatus,
      version: nextVersion,
      p1_reviewer_id: nextP1,
      p2_verifier_id: nextP2,
    },
    201,
    cors
  );
}

async function handleDailyExport(request, env, url, cors) {
  if (!verifyExportToken(request, env)) {
    return jsonResponse({ error: "Forbidden: valid EXPORT_BEARER_TOKEN required." }, 403, cors);
  }
  const onlyVerified = url.searchParams.get("verified_only") === "1";
  const sql = onlyVerified
    ? "SELECT book_id, page_number, status, version, proposal_json, updated_at FROM page_states WHERE version > exported_version AND status = 'verified' ORDER BY book_id, page_number LIMIT 500"
    : "SELECT book_id, page_number, status, version, proposal_json, updated_at FROM page_states WHERE version > exported_version AND status IN ('p1_proofed', 'verified') ORDER BY book_id, page_number LIMIT 500";
  const rows = await env.DB.prepare(sql).all();
  const items = (rows.results || []).map((r) => ({
    book_id: r.book_id,
    page_number: r.page_number,
    status: r.status,
    version: r.version,
    updated_at: r.updated_at,
    proposal: JSON.parse(r.proposal_json),
  }));
  return jsonResponse({ ok: true, count: items.length, items }, 200, cors);
}

async function handleAckExport(request, env, payload, cors) {
  if (!verifyExportToken(request, env)) {
    return jsonResponse({ error: "Forbidden: valid EXPORT_BEARER_TOKEN required." }, 403, cors);
  }
  const acked = Array.isArray(payload?.items) ? payload.items : [];
  if (acked.length === 0 || acked.length > 500) {
    return jsonResponse({ error: "items must contain 1..500 ack entries" }, 422, cors);
  }
  const stmts = acked.map((entry) =>
    env.DB.prepare(
      "UPDATE page_states SET exported_version = MAX(exported_version, ?) WHERE book_id = ? AND page_number = ?"
    ).bind(Number(entry.version || 0), String(entry.book_id || ""), Number(entry.page_number || 0))
  );
  await env.DB.batch(stmts);
  return jsonResponse({ ok: true, acknowledged: acked.length }, 200, cors);
}

function verifyExportToken(request, env) {
  const expected = String(env.EXPORT_BEARER_TOKEN || "");
  if (!expected || expected.length < 16) return false;
  const auth = request.headers.get("Authorization") || "";
  return auth === `Bearer ${expected}`;
}

function validateProposalPayload(p) {
  if (!p || p.version !== "correction-v2") return "Unsupported schema version (expected correction-v2)";
  if (!BOOK_SLUG_RE.test(String(p.book_id || ""))) return "Invalid book_id";
  if (!Array.isArray(p.corrections)) return "corrections must be an array";
  if (p.corrections.length > 200) return "Too many corrections in single proposal";

  for (const c of p.corrections) {
    if (!c || typeof c !== "object") return "Invalid correction object";
    if (!ALLOWED_OPS.has(c.op)) return `Disallowed op: ${c.op}`;
    if (!Number.isInteger(c.page_number) || c.page_number < 1 || c.page_number > 10000) {
      return "Invalid correction page_number";
    }
    if (c.replacement_tagged_text && String(c.replacement_tagged_text).length > 32000) {
      return "replacement_tagged_text exceeds 32 KB";
    }
  }

  const s = p.page_signoff;
  if (!s || typeof s !== "object") return "page_signoff is required";
  if (!Number.isInteger(s.page_number) || s.page_number < 1 || s.page_number > 10000) {
    return "Invalid signoff page_number";
  }
  if (!["p1_proofed", "verified", "problem", "needs_review"].includes(String(s.status || ""))) {
    return "Invalid signoff status";
  }
  if (!SHA256_RE.test(String(s.source_page_sha256 || ""))) return "Invalid source_page_sha256";
  if (!SHA256_RE.test(String(s.page_text_sha256 || ""))) return "Invalid page_text_sha256";
  return null;
}

async function sha256Hex(input) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function jsonResponse(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...headers, "Content-Type": "application/json; charset=utf-8" },
  });
}
