"""Reproducible reference-case audit. No manual 'passed' switch."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from cloud.library import canonical
from .model import now
from .registry import fingerprint
from .gate import release_reasons, LOCAL


def audit(store, import_id):
    registry=list(store.all(import_id,'court_entity'))
    cases=list(store.all(import_id,'court_case'))
    docs=list(store.all(import_id,'court_document'))
    expected=json.loads(Path(__file__).with_name('control_cases.json').read_text(encoding='utf-8'))
    missing=[];unexpected=[];proofs=set()
    for sample in expected:
        found=[c for c in cases if c['number']==sample['number'] and sample['inn'] in c.get('entity_ids',[])]
        if sample['participation']:
            found=[c for c in found if any(p.get('inn')==sample['inn'] and p.get('role')==sample['role']
                                          for p in c.get('participants',[]))]
            documents=[d for d in docs if any(d.get('case_id')==c['id'] for c in found)
                       and (not sample['date'] or d.get('date')==sample['date'])]
            if not found or not documents:missing.append(sample['number']+' / '+sample['inn'])
            else:
                for d in documents:proofs.update(d.get('evidence_ids',[]))
        elif found:unexpected.append(sample['number']+' / '+sample['inn'])
    proof_exists=proofs<=store.asset_ids(import_id)
    return {'registry':fingerprint(registry),'checked_at':now(),'passed':not missing and not unexpected and proof_exists,
            'known_missing':missing,'unexpected_participation':unexpected,'evidence_ids':sorted(proofs)}


def certify(store, import_id, at=None):
    """Only this explicit deployment command can enable public projection."""
    at=at or datetime.now(timezone.utc)
    registry=list(store.all(import_id,'court_entity'));control=audit(store,import_id)
    runs=list(store.all(import_id,'court_run'))
    reasons=release_reasons(registry,runs,control,at)
    if reasons:raise ValueError(' '.join(reasons))
    value={'enabled':True,'registry':fingerprint(registry),'certified_at':at.isoformat(),
           'accepted_days':[(at.astimezone(LOCAL).date()-timedelta(days=i)).isoformat() for i in range(1,15)],
           'control':control}
    store.query('''INSERT INTO skb_analytics.records(import_id,kind,key,payload)
        SELECT $1,'court_release','public',$2::jsonb WHERE EXISTS(
          SELECT 1 FROM skb_analytics.state WHERE key='active' AND import_id=$1)
        ON CONFLICT(import_id,kind,key) DO UPDATE SET payload=excluded.payload RETURNING key''',
        [import_id,canonical(value)],write=True)
    return value
