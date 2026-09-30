"""Audit supplied archives without touching collected products or prior analyses."""
from collections import Counter
from pathlib import Path
import csv
import hashlib
import io
import json
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price_monitor.characteristic_audit import audit_records
from price_monitor.notations import REGISTRY

INPUT=ROOT/'analysis/inductive_matching_2026_09_29/inputs/competitors'
OUTPUT=ROOT/'data/redesign_audit'


def records():
    inventory=[];rows=[]
    for file in sorted(INPUT.rglob('*.csv')):
        raw=file.read_bytes()
        try:text=raw.decode('utf-8-sig');encoding='utf-8-sig'
        except UnicodeDecodeError:text=raw.decode('cp1251');encoding='cp1251'
        separator=';' if text.splitlines()[0].count(';')>text.splitlines()[0].count(',') else ','
        reader=csv.reader(io.StringIO(text),delimiter=separator)
        columns=next(reader);count=Counter()
        for line,values in enumerate(reader,2):
            if len(values)==1 and len(columns)>1:
                nested=next(csv.reader([values[0]],delimiter=separator))
                if len(nested)==len(columns):values=nested;count['unwrapped']+=1
            if len(values)!=len(columns):count['rejected_shape']+=1;continue
            props={str(k).strip().rstrip(':'):v for k,v in zip(columns,values) if str(v).strip()}
            title=props.get('name','')
            if not title:count['without_name']+=1;continue
            source=file.parent.name
            fixed=next((brand for prefix,brand in [('balluff','Balluff'),('beskonta','BESKONTA'),('megak','МЕГА-К'),('sensor_com','СЕНСОР'),('teco','ТЕКО')] if source.startswith(prefix)),None)
            compact=re.sub(r'[^a-zа-я0-9]','',title.casefold())
            brand=fixed or next((b for b in REGISTRY if re.sub(r'[^a-zа-я0-9]','',b.casefold()) in compact),None)
            if not brand:count['outside_brand_scope']+=1;continue
            model=props.get('Артикул') or title
            if model==title:
                pattern=re.escape(brand).replace(r'\+',r'\s*\+?\s*')
                match=re.search(pattern,model,re.I)
                if match:model=model[match.end():].strip()
                model=re.sub(r'^(?:Индуктивный датчик|Датчик индуктивный|Датчик бесконтактный|Выключатель индуктивный)\s+','',model,flags=re.I)
            attrs=[{'name':k,'value':v} for k,v in props.items() if k not in ('name','price','info','link','is_available')]
            rows.append({'source':source,'manufacturer':brand,'article':model,'title':title,
                         'product_url':props.get('link',''),'_specifications':{'attributes':attrs},
                         '_archive_file':str(file.relative_to(ROOT)),'_archive_row':line})
            count['accepted']+=1
        inventory.append({'file':str(file.relative_to(ROOT)),'sha256':hashlib.sha256(raw).hexdigest(),
                          'encoding':encoding,'columns':len(columns),**count})
    return rows,inventory


def main():
    rows,inventory=records()
    summary,issues=audit_records(rows)
    OUTPUT.mkdir(parents=True,exist_ok=True)
    payload={'scope':'Supplied historical CSV archives; not a live database audit. Grain: source CSV record; duplicates retained and counted.',
             'collection_dates':'Not provided in these files; freshness cannot be established.',
             'brand_attribution':'Dedicated archive name or explicit manufacturer in title; not proof of current manufacturer identity.',
             'normalization':'Empty cells are omitted. Only inductive products are assessed for matching completeness. Missing characteristics may originate in extraction or export; live re-collection needed to distinguish.',
             'inventory':inventory,'summary':summary,'issues':issues}
    (OUTPUT/'characteristics.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(inventory),'rows':len(rows),'brands':len(summary),
                      'malformed_rows':sum(r.get('rejected_shape',0) for r in inventory),'summary':summary},ensure_ascii=False))


if __name__=='__main__':main()
