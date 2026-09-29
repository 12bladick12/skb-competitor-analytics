"""Data-only handoff of Sensoren snapshots over short TLS connections.

All server operations below are fixed SQL. No uploaded data is executable SQL.
Functions use the existing database role and have no public API permissions.
"""
from dataclasses import asdict
import hashlib
import json
import uuid

TABLES = '''
CREATE TABLE IF NOT EXISTS sensoren_payloads (
 id TEXT PRIMARY KEY, checksum TEXT NOT NULL, parts INTEGER NOT NULL,
 accepted INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sensoren_payload_parts (
 payload_id TEXT NOT NULL REFERENCES sensoren_payloads(id) ON DELETE CASCADE,
 part INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(payload_id,part)
);
REVOKE ALL ON sensoren_payloads,sensoren_payload_parts FROM PUBLIC;
'''

SAVE_OBSERVATION = '''CREATE OR REPLACE FUNCTION sensoren_write_observation(jid BIGINT,item JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path=price_monitor,pg_temp AS $fn$
DECLARE qid BIGINT; o JSONB:=item->'observation'; doc TEXT:=item->>'document';
 fp TEXT:=item->>'fingerprint'; ref TEXT:='{}';
BEGIN
 SELECT j.rule_id INTO STRICT qid FROM jobs j JOIN rules q ON q.id=j.rule_id WHERE j.id=jid AND q.source='sensoren';
 IF fp<>'' THEN
  INSERT INTO product_documents VALUES(fp,doc) ON CONFLICT DO NOTHING;
  ref:=jsonb_build_object('ref',fp)::text;
  INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at)
   SELECT qid,o->>'title',coalesce(doc::jsonb->>'category',''),
    lower(q.source||' '||q.manufacturer||' '||q.article||' '||(o->>'title')),fp,
    jsonb_array_length(coalesce(doc::jsonb->'attributes','[]'::jsonb)),o->>'checked_at' FROM rules q WHERE q.id=qid
   ON CONFLICT(rule_id) DO UPDATE SET title=excluded.title,category=excluded.category,
    search_text=excluded.search_text,details_hash=excluded.details_hash,
    attributes_count=excluded.attributes_count,updated_at=excluded.updated_at;
 END IF;
 INSERT INTO observations(job_id,status,url,title,price,currency,availability,price_text,availability_text,
  detail,checked_at,http_status,response_hash,details_json)
 VALUES(jid,o->>'status',o->>'url',o->>'title',o->>'price',o->>'currency',o->>'availability',
  o->>'price_text',o->>'availability_text',o->>'detail',o->>'checked_at',(o->>'http_status')::integer,o->>'response_hash',ref)
 ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,url=excluded.url,title=excluded.title,
  price=excluded.price,currency=excluded.currency,availability=excluded.availability,
  price_text=excluded.price_text,availability_text=excluded.availability_text,detail=excluded.detail,
  checked_at=excluded.checked_at,http_status=excluded.http_status,response_hash=excluded.response_hash,
  details_json=excluded.details_json WHERE observations.status NOT IN ('priced','on_request','no_price');
 UPDATE jobs SET state='done' WHERE id=jid;
END $fn$;
REVOKE ALL ON FUNCTION sensoren_write_observation(BIGINT,JSONB) FROM PUBLIC;'''

SAVE_CATALOG = '''CREATE OR REPLACE FUNCTION sensoren_store_catalog(data JSONB) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path=price_monitor,pg_temp AS $fn$
DECLARE rid BIGINT:=(data->>'run')::bigint; pid TEXT:=data->>'page'; item JSONB; qid BIGINT; jid BIGINT;
 brand TEXT; fresh INTEGER:=0; errors BOOLEAN:=false; stamp TEXT; link TEXT;
 month_start TEXT:=to_char(now() AT TIME ZONE 'UTC','YYYY-MM')||'-01T00:00:00+00:00';
 month_end TEXT:=to_char((date_trunc('month',now() AT TIME ZONE 'UTC')+interval '1 month'),'YYYY-MM-DD')||'T00:00:00+00:00';
BEGIN
 IF jsonb_typeof(data->'products') IS DISTINCT FROM 'array' OR jsonb_array_length(data->'products') NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'Invalid product count'; END IF;
 IF NOT EXISTS(SELECT 1 FROM catalog_pages WHERE id=pid AND run_id=rid AND source='sensoren' AND kind='product') THEN RETURN false; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(data->'products') LOOP
  brand:=item->'rule'->>'manufacturer';
  IF item->'rule'->>'source' IS DISTINCT FROM 'sensoren' OR NOT EXISTS(SELECT 1 FROM catalog_sources s,
   jsonb_array_elements_text(s.brands_json::jsonb) b WHERE s.run_id=rid AND s.source='sensoren' AND b=brand)
   THEN RAISE EXCEPTION 'Product manufacturer is outside selected catalog'; END IF;
  IF item->'observation'->>'status' NOT IN ('priced','on_request','no_price') THEN errors:=true; END IF;
  SELECT id INTO qid FROM rules WHERE rule_key=item->>'key';
  IF qid IS NOT NULL AND EXISTS(SELECT 1 FROM jobs j JOIN observations o ON o.job_id=j.id
   WHERE j.rule_id=qid AND o.status IN ('priced','on_request','no_price') AND o.http_status=200
   AND o.details_json<>'{}' AND o.checked_at>=month_start AND o.checked_at<month_end) THEN CONTINUE; END IF;
  INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at)
   VALUES(item->>'key','sensoren',brand,item->'rule'->>'article',item->'rule'->>'product_url',
    item->'rule'->>'url_template',data->>'now') ON CONFLICT(rule_key) DO NOTHING;
  SELECT id INTO STRICT qid FROM rules WHERE rule_key=item->>'key';
  INSERT INTO jobs(run_id,rule_id,state) VALUES(rid,qid,'processing') ON CONFLICT DO NOTHING;
  SELECT id INTO STRICT jid FROM jobs WHERE run_id=rid AND rule_id=qid;
  PERFORM sensoren_write_observation(jid,item);fresh:=fresh+1;
 END LOOP;
 IF NOT errors AND jsonb_array_length(data->'products')>0 THEN
  SELECT min(value->'observation'->>'checked_at') INTO stamp FROM jsonb_array_elements(data->'products');
  FOR link IN SELECT value FROM jsonb_array_elements_text(data->'urls') LOOP
   IF link NOT LIKE 'https://sensoren.ru/%' THEN RAISE EXCEPTION 'Invalid product URL'; END IF;
   INSERT INTO monthly_page_memory(source,url,checked_at,revision) VALUES('sensoren',link,stamp,1)
    ON CONFLICT(source,url) DO UPDATE SET checked_at=excluded.checked_at,revision=excluded.revision
    WHERE excluded.checked_at>=monthly_page_memory.checked_at;
  END LOOP;
 END IF;
 UPDATE catalog_pages SET state=CASE WHEN errors THEN 'failed' WHEN fresh>0 THEN 'done' ELSE 'cached' END,
  detail=CASE WHEN errors THEN 'Product parsing error' ELSE '' END,http_status=200,checked_at=data->>'now' WHERE id=pid;
 RETURN true;
END $fn$;
REVOKE ALL ON FUNCTION sensoren_store_catalog(JSONB) FROM PUBLIC;'''

APPLY_PAYLOAD = '''CREATE OR REPLACE FUNCTION sensoren_apply_payload(payload_key TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER SET search_path=price_monitor,pg_temp AS $fn$
DECLARE meta sensoren_payloads%ROWTYPE; body TEXT; count_parts INTEGER; data JSONB; rid BIGINT; jid BIGINT; ok BOOLEAN;
BEGIN
 SELECT * INTO STRICT meta FROM sensoren_payloads WHERE id=payload_key FOR UPDATE;
 IF meta.accepted IS NOT NULL THEN RETURN meta.accepted=1; END IF;
 SELECT string_agg(p.body,'' ORDER BY p.part),count(*) INTO body,count_parts FROM sensoren_payload_parts p WHERE p.payload_id=payload_key;
 IF count_parts<>meta.parts OR md5(body)<>meta.checksum THEN RAISE EXCEPTION 'Incomplete Sensoren payload'; END IF;
 data:=body::jsonb;rid:=(data->>'run')::bigint;
 PERFORM pg_advisory_xact_lock(6743928101);
 IF NOT EXISTS(SELECT 1 FROM external_sources WHERE source='sensoren' AND enabled=1 AND owner=data->>'owner'
  AND heartbeat>EXTRACT(EPOCH FROM clock_timestamp())-120 AND protocol>=2)
  OR NOT EXISTS(SELECT 1 FROM runs WHERE id=rid AND state='running' AND cancel_requested=0)
  THEN UPDATE sensoren_payloads SET accepted=0 WHERE id=payload_key; RETURN false; END IF;
 IF data->>'mode'='catalog' THEN
  ok:=sensoren_store_catalog(data);
 ELSIF data->>'mode'='job' THEN
  jid:=(data->>'job')::bigint;
  ok:=EXISTS(SELECT 1 FROM jobs j JOIN rules q ON q.id=j.rule_id WHERE j.id=jid AND j.run_id=rid AND q.source='sensoren' AND j.state='processing');
  IF ok THEN
   PERFORM sensoren_write_observation(jid,data->'product');
   IF coalesce(data->>'stop_reason','')<>'' THEN INSERT INTO source_pauses VALUES(rid,'sensoren',data->>'stop_reason') ON CONFLICT DO NOTHING; END IF;
  END IF;
 ELSE RAISE EXCEPTION 'Unknown Sensoren payload type'; END IF;
 UPDATE sensoren_payloads SET accepted=CASE WHEN ok THEN 1 ELSE 0 END WHERE id=payload_key;
 RETURN ok;
END $fn$;
REVOKE ALL ON FUNCTION sensoren_apply_payload(TEXT) FROM PUBLIC;'''

SCHEMA=[TABLES,SAVE_OBSERVATION,SAVE_CATALOG,APPLY_PAYLOAD]


def utf8_parts(value, maximum=2200):
    if maximum<4:raise ValueError('UTF-8 chunks must allow a complete code point')
    data=value.encode('utf-8')
    while data:
        end=min(maximum,len(data))
        while end<len(data) and data[end]&0xc0==0x80:end-=1
        yield data[:end].decode('utf-8');data=data[end:]


def product_payload(rule,observation):
    from .catalog_schema import document
    fingerprint,canonical,_=document(observation.details_json)
    values=asdict(observation);values.pop('details_json')
    item={'observation':values,'fingerprint':fingerprint,'document':canonical}
    if rule is not None:item.update(rule=asdict(rule),key=rule.key)
    return item


def save_payload(settings,data):
    from .db_batches import connection_for
    from .db_connection import DatabaseIOTimeout
    from .runtime import phase
    settings={**settings,'reuse_connections':False}
    payload=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    parts=list(utf8_parts(payload));key=str(uuid.uuid4())
    def short(sql,values=None):
        with connection_for(settings) as connection:
            connection.io_timeout=15
            cursor=connection.execute("BEGIN; SET LOCAL search_path=price_monitor; SET LOCAL statement_timeout='10s'; "
                "SET LOCAL lock_timeout='8s'; "+sql+'; COMMIT',values or {})
            rows=[]
            while True:
                if cursor.description:rows=cursor.fetchall()
                if not cursor.nextset():break
            return rows
    with phase('save'):
        short('INSERT INTO sensoren_payloads(id,checksum,parts) VALUES(%(id)s,%(checksum)s,%(parts)s)',
              {'id':key,'checksum':hashlib.md5(payload.encode()).hexdigest(),'parts':len(parts)})
        for index,part in enumerate(parts):
            short('INSERT INTO sensoren_payload_parts VALUES(%(id)s,%(part)s,%(body)s) ON CONFLICT DO NOTHING',
                  {'id':key,'part':index,'body':part})
        try:result=short('SELECT sensoren_apply_payload(%(id)s) accepted',{'id':key})
        except DatabaseIOTimeout:result=short('SELECT sensoren_apply_payload(%(id)s) accepted',{'id':key})
        accepted=bool(result[0]['accepted'])
        try:short('DELETE FROM sensoren_payloads WHERE id=%(id)s',{'id':key})
        except DatabaseIOTimeout:pass
        return accepted


def save_catalog(settings,run_id,page_id,results,owner,page_url):
    from .models import utcnow
    from .monthly import page_key
    urls={page_key('sensoren',result.url) for _,result in results};urls.add(page_key('sensoren',page_url))
    return save_payload(settings,{'mode':'catalog','run':run_id,'page':page_id,'owner':owner,'now':utcnow(),
        'urls':sorted(urls),'products':[product_payload(rule,obs) for rule,obs in results]})
