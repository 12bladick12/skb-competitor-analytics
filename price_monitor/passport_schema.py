"""Additive, SQLite/PostgreSQL compatible catalog and passport state."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS product_lifecycle (
 rule_id INTEGER PRIMARY KEY REFERENCES rules(id), state TEXT NOT NULL DEFAULT 'active',
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, checked_at TEXT NOT NULL,
 missing_since TEXT, last_missing_check TEXT, missing_count INTEGER NOT NULL DEFAULT 0,
 missing_kind TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '', current_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS product_aliases (
 source TEXT NOT NULL, url TEXT NOT NULL, article TEXT NOT NULL,
 rule_id INTEGER NOT NULL REFERENCES rules(id), PRIMARY KEY(source,url,article)
);
CREATE TABLE IF NOT EXISTS catalog_integrity (
 run_id INTEGER NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
 state TEXT NOT NULL, brands_json TEXT NOT NULL, checked_at TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id,source)
);
CREATE TABLE IF NOT EXISTS product_events (
 id TEXT PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES rules(id),
 kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_variant_sets (
 run_id INTEGER NOT NULL REFERENCES runs(id),source TEXT NOT NULL,url TEXT NOT NULL,
 articles_json TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(run_id,source,url)
);
CREATE INDEX IF NOT EXISTS product_events_rule ON product_events(rule_id,created_at);
CREATE TABLE IF NOT EXISTS passport_products (
 rule_id INTEGER PRIMARY KEY REFERENCES rules(id), state TEXT NOT NULL DEFAULT 'pending',
 checked_at TEXT NOT NULL DEFAULT '', input_hash TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '', current_fingerprint TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '', etag TEXT NOT NULL DEFAULT '',
 last_modified TEXT NOT NULL DEFAULT ''
 ,scheduled_period TEXT NOT NULL DEFAULT '', scheduled_input TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS passport_card_scans (
 source TEXT NOT NULL,url TEXT NOT NULL,period TEXT NOT NULL,version TEXT NOT NULL,
 candidates_json TEXT NOT NULL,checked_at TEXT NOT NULL,
 PRIMARY KEY(source,url,period,version)
);
CREATE TABLE IF NOT EXISTS passport_candidates (
 rule_id INTEGER NOT NULL REFERENCES rules(id),url TEXT NOT NULL,name TEXT NOT NULL,
 kind TEXT NOT NULL,source_page TEXT NOT NULL,state TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
 checked_at TEXT NOT NULL,PRIMARY KEY(rule_id,url)
);
CREATE TABLE IF NOT EXISTS passport_files (
 fingerprint TEXT PRIMARY KEY, object_key TEXT NOT NULL, byte_size INTEGER NOT NULL,
 page_count INTEGER NOT NULL, kind TEXT NOT NULL, language TEXT NOT NULL,
 revision_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passport_links (
 rule_id INTEGER NOT NULL REFERENCES rules(id), fingerprint TEXT NOT NULL REFERENCES passport_files(fingerprint),
 url TEXT NOT NULL, applicability TEXT NOT NULL, evidence TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(rule_id,fingerprint,url)
);
CREATE TABLE IF NOT EXISTS passport_jobs (
 id TEXT PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES rules(id), kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 owner TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0,
 not_before INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS passport_queue ON passport_jobs(state,not_before,kind);
CREATE TABLE IF NOT EXISTS passport_job_metrics (
 job_id TEXT NOT NULL REFERENCES passport_jobs(id),attempt INTEGER NOT NULL,
 requests INTEGER NOT NULL DEFAULT 0,received_bytes INTEGER NOT NULL DEFAULT 0,
 pages INTEGER NOT NULL DEFAULT 0,seconds REAL NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS passport_geometry (
 fingerprint TEXT NOT NULL REFERENCES passport_files(fingerprint), version TEXT NOT NULL,
 result_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'review', reviewed_json TEXT NOT NULL DEFAULT '{}',
 reviewer TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 PRIMARY KEY(fingerprint,version)
);
CREATE TABLE IF NOT EXISTS passport_field_reviews (
 rule_id INTEGER NOT NULL REFERENCES rules(id), fingerprint TEXT NOT NULL,
 fields_json TEXT NOT NULL, reviewer TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(rule_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS passport_workers (
 owner TEXT PRIMARY KEY, heartbeat INTEGER NOT NULL, state TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT ''
);
"""
