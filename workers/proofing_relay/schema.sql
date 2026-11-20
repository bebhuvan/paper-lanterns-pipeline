-- Paper Lanterns v2 — Distributed Proofreaders (P1 -> P2) Cloudflare D1 Schema
-- Holds work-in-progress leases, P1/P2 sign-offs, bounded anonymous flags, and daily Git export cursor.
-- Canonical truth lives in Git (books/<book_id>/corrections/ and proof_ledger.json).

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'proofreader' CHECK (role IN ('proofreader', 'trusted', 'maintainer')),
    credit_name TEXT,
    suspended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_leases (
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    claimed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (book_id, page_number)
);

CREATE TABLE IF NOT EXISTS page_states (
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unproofed' CHECK (status IN ('unproofed', 'p1_proofed', 'verified', 'problem')),
    version INTEGER NOT NULL DEFAULT 0,
    source_page_sha256 TEXT NOT NULL,
    page_text_sha256 TEXT NOT NULL,
    p1_reviewer_id TEXT,
    p2_verifier_id TEXT,
    proposal_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    exported_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, page_number)
);

CREATE TABLE IF NOT EXISTS proof_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    source_page_sha256 TEXT NOT NULL,
    page_text_sha256 TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_flags (
    flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    note TEXT,
    created_day TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day_key TEXT NOT NULL,
    counter_name TEXT NOT NULL,
    count_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day_key, counter_name)
);

CREATE INDEX IF NOT EXISTS idx_page_states_export
    ON page_states (status, version, exported_version);

CREATE INDEX IF NOT EXISTS idx_proof_events_book_page
    ON proof_events (book_id, page_number, event_id);
