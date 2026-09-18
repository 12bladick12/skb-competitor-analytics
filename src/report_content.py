"""Read-only, shared projection for documents and the web application."""
import json
from pathlib import Path

from .evidence_store import EvidenceStore


def prepare_report_data(storage, competitors, period):
    store = EvidenceStore(storage, initialize=False)
    codes = [c.code for c in competitors]
    events = store.events(period, codes)
    events = [e for e in events if evidence_exists(e)]
    checks = store.coverage(period, codes)
    links = {}
    for row in storage.conn.execute('SELECT * FROM event_link_checks ORDER BY run_id DESC'):
        links.setdefault(row['event_id'], dict(row))
    return events, checks, links


def evidence_exists(event):
    try:
        paths = json.loads(event['evidence_json']).get('snapshots', [])
        return bool(paths) and all(Path(p).is_file() for p in paths)
    except (ValueError, TypeError, OSError):
        return False
