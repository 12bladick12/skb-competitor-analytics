"""Small, robots-aware audit of public source pages. No challenge bypass."""
from pathlib import Path
import sys,json,csv,hashlib
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from price_monitor.transport import SourceClient,FetchError
from price_monitor.details import attributes,manufacturer

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'audit_20260929'
OUT.mkdir(parents=True,exist_ok=True)

def audit(source,urls):
    client=SourceClient(source,delay=3)
    original=client._request
    def observed(url,*args,**kwargs):
        status,headers,body=original(url,*args,**kwargs)
        soup=BeautifulSoup(body,'html.parser')
        key=source+'_'+hashlib.sha256(url.encode()).hexdigest()[:12]
        (OUT/(key+'.html')).write_text(body,encoding='utf-8')
        safe_headers={k:v for k,v in headers.items() if k.lower() in
            ('server','content-type','retry-after','location','via','x-powered-by','x-cache','x-mitmproxy-blocked-reason')}
        entry={'source':source,'url':url,'http':status,'headers':safe_headers,'bytes':len(body.encode()),
            'title':soup.title.get_text(' ',strip=True) if soup.title else '',
            'h1':soup.h1.get_text(' ',strip=True) if soup.h1 else '',
            'attributes':attributes(source,soup),'manufacturer':manufacturer(source,soup),
            'body_file':key+'.html'}
        with (OUT/'responses.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
        print(json.dumps({k:v for k,v in entry.items() if k!='attributes'}|{'attributes_count':len(entry['attributes'])},ensure_ascii=False),flush=True)
        return status,headers,body
    client._request=observed
    try:
        for url in urls:
            try:client.fetch_document(url)
            except FetchError as error:
                print(json.dumps({'source':source,'url':url,'error':str(error),'status':error.status},ensure_ascii=False),flush=True)
                if error.stop_source:break
    finally:client.close()

if __name__=='__main__':
    examples=list(csv.DictReader((ROOT/'examples/tasks.csv').open(encoding='utf-8-sig'),delimiter=';'))
    sensor=['https://sensoren.ru/catalog/','https://sensoren.ru/product/induktivnyy_datchik_lanbao_le05vf08dlc_f1/']
    sensor += [r['product_url'] for r in examples if r['source']=='sensoren']
    teko=['https://teko-com.ru/catalog/','https://teko-com.ru/catalog/product/cp-s254r-3-pg9/']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(audit,*args) for args in [('sensoren',sensor),('teko',teko)]]
        for future in futures:future.result()
