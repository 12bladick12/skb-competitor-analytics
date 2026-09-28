"""Additive schema for durable catalogs, product specifications and comparisons."""
SCHEMA = """
CREATE INDEX IF NOT EXISTS rules_catalog_search ON rules(source,manufacturer,article);
CREATE TABLE IF NOT EXISTS catalog_sources (
 run_id INTEGER NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
 brands_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 detail TEXT NOT NULL DEFAULT '', started_at TEXT, finished_at TEXT,
 PRIMARY KEY(run_id,source)
);
CREATE TABLE IF NOT EXISTS catalog_pages (
 id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
 source TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
 http_status INTEGER, checked_at TEXT,
 UNIQUE(run_id,source,url)
);
CREATE INDEX IF NOT EXISTS catalog_queue ON catalog_pages(run_id,source,state,kind);
CREATE TABLE IF NOT EXISTS product_documents (
 fingerprint TEXT PRIMARY KEY, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_index (
 rule_id INTEGER PRIMARY KEY REFERENCES rules(id), title TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL DEFAULT '',
 details_hash TEXT NOT NULL DEFAULT '', attributes_count INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_items (
 rule_id INTEGER PRIMARY KEY REFERENCES rules(id), our_article TEXT NOT NULL DEFAULT '',
 our_price TEXT, our_currency TEXT NOT NULL DEFAULT 'RUB',
 note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
"""


def document(details_json):
    import hashlib
    import json
    payload=json.loads(details_json or '{}')
    if not payload or 'ref' in payload:return '', '{}', payload
    canonical=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return hashlib.sha256(canonical.encode()).hexdigest(),canonical,payload


def index_product(connection, job_id, observation):
    import json
    fingerprint, canonical, payload=document(observation.details_json)
    if not fingerprint:return
    connection.execute('INSERT INTO product_documents(fingerprint,details_json) VALUES(?,?) ON CONFLICT(fingerprint) DO NOTHING',(fingerprint,canonical))
    connection.execute('''INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at)
        SELECT q.id,?, ?,lower(q.source || ' ' || q.manufacturer || ' ' || q.article || ' ' || ?),?,?,?
        FROM rules q JOIN jobs j ON j.rule_id=q.id WHERE j.id=?
        ON CONFLICT(rule_id) DO UPDATE SET title=excluded.title,category=excluded.category,
        search_text=excluded.search_text,details_hash=excluded.details_hash,
        attributes_count=excluded.attributes_count,updated_at=excluded.updated_at''',
        (observation.title,payload.get('category',''),observation.title+' '+payload.get('category',''),
         fingerprint,len(payload.get('attributes',[])),observation.checked_at,job_id))
    return json.dumps({'ref':fingerprint})
