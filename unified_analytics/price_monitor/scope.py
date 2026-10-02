"""Manufacturer evidence for mixed-brand dealer catalogs; history is retained."""
import json
from .details import manufacturer_from_details

VISIBLE = """(q.source<>'teko' OR EXISTS(SELECT 1 FROM product_scope verified
    JOIN product_index verified_index ON verified_index.rule_id=verified.rule_id
      AND verified_index.details_hash=verified.details_hash
    WHERE verified.rule_id=q.id AND verified.state='confirmed' AND verified.manufacturer='ТЕКО'))"""

# Keep diagnostic job outcomes visible even when the product brand was not
# confirmed. Only verified products can appear in the searchable library.
RESULT_VISIBLE = '('+VISIBLE+" OR COALESCE(o.status,'pending') NOT IN ('priced','on_request','no_price'))"

def refresh_scope(repository, max_batches=None):
    """Backfill only changed/missing evidence from already saved specifications."""
    count=0;batches=0
    size=10 if repository.settings and not repository.settings.get('reuse_connections',True) else 50
    payload='d.details_json'
    if repository.settings:
        # Backfill needs only brand evidence, not descriptions and documents.
        # Keep responses small on remote connections and large catalogs.
        payload="""jsonb_build_object('attributes',COALESCE((
            SELECT jsonb_agg(prop) FROM jsonb_array_elements(d.details_json::jsonb->'attributes') prop
            WHERE lower(rtrim(btrim(prop->>'name'),':')) IN ('бренд','производитель','manufacturer','brand')
            ),'[]'::jsonb),'manufacturer',d.details_json::jsonb->>'manufacturer')::text AS details_json"""
    while True:
        rows=repository.batch('''SELECT q.id,i.details_hash,i.updated_at,'''+payload+'''
            FROM rules q JOIN product_index i ON i.rule_id=q.id
            JOIN product_documents d ON d.fingerprint=i.details_hash
            WHERE q.source='teko' AND NOT EXISTS
                (SELECT 1 FROM product_scope v WHERE v.rule_id=q.id AND v.details_hash=i.details_hash)
            ORDER BY q.id LIMIT %(limit)s''',{'limit':size})
        if not rows:break
        values=[];params={}
        for n,row in enumerate(rows):
            brand=manufacturer_from_details(json.loads(row['details_json']))
            data={'id':row['id'],'brand':brand,'state':'confirmed' if brand=='ТЕКО' else ('excluded' if brand else 'unverified'),
                  'hash':row['details_hash'],'time':row['updated_at']}
            params.update({f'{key}{n}':value for key,value in data.items()})
            values.append('('+','.join(f'%({key}{n})s' for key in data)+')')
        repository.batch('INSERT INTO product_scope(rule_id,manufacturer,state,details_hash,checked_at) VALUES '+','.join(values)+
            ' ON CONFLICT(rule_id) DO UPDATE SET manufacturer=excluded.manufacturer,state=excluded.state,details_hash=excluded.details_hash,checked_at=excluded.checked_at',params)
        count+=len(rows)
        batches+=1
        if max_batches is not None and batches>=max_batches:break
    return count
