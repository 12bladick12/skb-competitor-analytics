"""Read-only catalog snapshot and reproducible completeness audit; no document jobs."""
import argparse,json,sys,time
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter,defaultdict
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.matching_normalize import normalize_sensor
from price_monitor.matching import MANDATORY,IMPORTANT,FIELD_LABELS

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'analysis'/'inductive_audit_2026_10_01'


def export():
    from price_monitor.catalog_storage import CatalogRepository
    from price_monitor.sensoren_local import settings_from_file
    from price_monitor.scope import VISIBLE
    repo=CatalogRepository(settings={**settings_from_file(ROOT/'.streamlit/secrets.toml'),'reuse_connections':False})
    OUT.mkdir(parents=True,exist_ok=True)
    sql='''SELECT q.id rule_id,q.source,q.manufacturer,q.article,q.product_url,i.title,i.category,
        i.updated_at,i.attributes_count,d.details_json,COALESCE(l.state,'active') product_state
        FROM (SELECT q.* FROM rules q WHERE '''+VISIBLE+''' AND q.id>%(last)s AND q.id<=%(end)s
              ORDER BY q.id LIMIT 250 OFFSET 0) q LEFT JOIN product_index i ON i.rule_id=q.id
        LEFT JOIN product_documents d ON d.fingerprint=i.details_hash
        LEFT JOIN product_lifecycle l ON l.rule_id=q.id
        ORDER BY q.id'''
    meta={'started_at':datetime.now(timezone.utc).isoformat(),'sql':sql,'visible_predicate':VISIBLE,
          'grain':'one rule_id / source, manufacturer, article, URL',
          'brands':repo.batch('SELECT q.manufacturer,count(*) total FROM rules q WHERE '+VISIBLE+' GROUP BY q.manufacturer ORDER BY q.manufacturer')}
    end=repo.batch('SELECT max(id) id FROM rules')[0]['id'];last=0;total=0
    print(json.dumps(meta['brands'],ensure_ascii=True),flush=True)
    with (OUT/'records.jsonl').open('w',encoding='utf-8') as out:
        while True:
            for attempt in range(3):
                try:rows=repo.batch(sql,{'last':last,'end':end});break
                except Exception:
                    if attempt==2:raise
            if not rows:break
            for row in rows:
                row['_specifications']=json.loads(row.pop('details_json') or '{}')
                out.write(json.dumps(row,ensure_ascii=False)+'\n')
            last=rows[-1]['rule_id'];total+=len(rows)
            out.flush()
            if total%1000==0 or total==250:print('exported',total,flush=True)
    meta.update(finished_at=datetime.now(timezone.utc).isoformat(),rows=total,max_rule_id=end)
    (OUT/'snapshot.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print('exported_total',total,flush=True)


def profile(label):
    buckets=defaultdict(Counter);names=defaultdict(Counter);examples=defaultdict(list);missing_examples=defaultdict(list)
    details=[];seen=set()
    for line in (OUT/'records.jsonl').open(encoding='utf-8'):
        row=json.loads(line);brand=row['manufacturer'];b=buckets[brand];b['rows']+=1
        b['duplicate_id']+=row['rule_id'] in seen;seen.add(row['rule_id'])
        sensor=normalize_sensor(row)
        b['family_'+str(sensor.family)]+=1
        if sensor.family!='inductive':continue
        b['inductive']+=1
        fields=list(dict.fromkeys((*MANDATORY,*IMPORTANT,'length','wire_count')))
        if sensor.values.get('body_type') in (None,'threaded'):fields.append('pitch')
        if sensor.values.get('connection') in ('connector','cable+connector'):fields.extend(['connector','pin_count'])
        raw=normalize_sensor(row,enrich_designation=False)
        attrs=row['_specifications'].get('attributes',[])
        b['without_attributes']+=not attrs;b['special']+=bool(sensor.special)
        b['supported']+=bool(sensor.decoding.get('supported'));b['review']+=bool(sensor.decoding.get('requires_review'))
        b['conflicts']+=bool(sensor.conflicts) or any(e.get('application')=='conflict' for e in sensor.decoding.get('fields',{}).values())
        for a in attrs:names[brand][a.get('name','')]+=1
        for f in fields:
            b['applicable_'+f]+=1;b['raw_'+f]+=raw.values.get(f) is not None;b['filled_'+f]+=sensor.values.get(f) is not None
            if sensor.values.get(f) is None and len(missing_examples[(brand,f)])<4:missing_examples[(brand,f)].append({'article':row['article'],'url':row['product_url'],'attributes':attrs})
        required=list(MANDATORY)+(['pitch'] if sensor.values.get('body_type') in (None,'threaded') else [])
        b['mandatory_complete']+=all(sensor.values.get(f) is not None for f in required)
        b['all_complete']+=all(sensor.values.get(f) is not None for f in fields)
        if len(examples[brand])<20:examples[brand].append({'article':row['article'],'url':row['product_url'],'attributes':attrs,'values':sensor.values})
        details.append({'rule_id':row['rule_id'],'brand':brand,'article':row['article'],'special':sensor.special,'values':sensor.values,'raw_values':raw.values,
                        'missing':[f for f in fields if sensor.values.get(f) is None],'decoding':sensor.decoding})
    result={'brands':dict(buckets),'attribute_names':{b:dict(c.most_common()) for b,c in names.items()},'examples':dict(examples),
            'missing_examples':{b+'|'+f:v for (b,f),v in missing_examples.items()}}
    (OUT/(label+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/(label+'_products.jsonl')).write_text('\n'.join(json.dumps(v,ensure_ascii=False) for v in details)+'\n',encoding='utf-8')
    print(json.dumps({b:{k:v for k,v in c.items() if not k.startswith(('raw_','filled_','applicable_'))} for b,c in buckets.items()},ensure_ascii=True,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--export',action='store_true');parser.add_argument('--profile')
    parser.add_argument('--snapshot-dir',type=Path,default=OUT);args=parser.parse_args();OUT=args.snapshot_dir
    if args.export:export()
    if args.profile:profile(args.profile)
