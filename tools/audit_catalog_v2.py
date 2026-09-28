"""Bounded, read-only catalog audit. Evidence remains under ignored data/."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bs4 import BeautifulSoup
from price_monitor.sources import SOURCES
from price_monitor.transport import SourceClient

OUT=ROOT/'data'/'catalog_audit_v2'
OUT.mkdir(parents=True,exist_ok=True)
CATALOG={'sensoren':'/catalog/','beskonta':'/catalog/all/','megak':'/catalog','teko':'/catalog/datchiki/','sensor':'/'}
fixtures=json.loads((ROOT/'tests'/'fixtures'/'manifest.json').read_text(encoding='utf-8'))

def audit(source):
    spec=SOURCES[source];base='https://'+spec.host;client=SourceClient(source)
    evidence={'source':source,'pages':[]}
    try:
        code,headers,body=client._request(base+'/robots.txt')
        client._check_status(code,headers,body,base+'/robots.txt','robots.txt')
        (OUT/(source+'_robots.txt')).write_text(body,encoding='utf-8')
        evidence['robots_status']=code
        maps=re.findall(r'^Sitemap:\s*(\S+)',body,re.I|re.M) or [base+'/sitemap.xml']
        evidence['sitemaps']=maps
        queue=maps[:1]
        for index in range(3):
            if not queue:break
            url=queue.pop(0);url=client.redirect(base,url)
            try:
                client.policy(url)
                status,head,text=client._request(url)
                client._check_status(status,head,text,url,'sitemap')
                (OUT/f'{source}_sitemap_{index}.xml').write_text(text,encoding='utf-8')
                xml=ET.fromstring(text)
                links=[x.text for x in xml.iter() if x.tag.split('}')[-1]=='loc' and x.text]
                evidence['pages'].append({'kind':'sitemap','url':url,'status':status,'root':xml.tag,'count':len(links),'examples':links[:12]})
                if xml.tag.split('}')[-1]=='sitemapindex':
                    links.sort(key=lambda v:(not any(k in v.lower() for k in ('product','catalog','iblock-2','iblock_2')),v))
                    queue.extend(links[:2])
            except Exception as exc:evidence['pages'].append({'url':url,'error':type(exc).__name__,'detail':str(exc)[:200]})
        paths=[('catalog',base+CATALOG[source])]
        paths += [('product_'+str(i),x['product_url']) for i,x in enumerate(fixtures) if x['source']==source][:2]
        for kind,url in paths:
            try:
                final,status,html=client.fetch(url)
                (OUT/f'{source}_{kind}.html').write_text(html,encoding='utf-8')
                soup=BeautifulSoup(html,'html.parser')
                links=[{'text':a.get_text(' ',strip=True)[:90],'href':urljoin(final,a['href'])} for a in soup.select('a[href]')]
                useful=[x for x in links if any(k in x['href'].lower() for k in ('catalog','product','sensors','pagen','page='))]
                evidence['pages'].append({'kind':kind,'url':final,'status':status,'title':soup.h1.get_text(' ',strip=True) if soup.h1 else '', 'links':list({x['href']:x for x in useful}.values())[:40], 'tables':len(soup.select('table')),'hints':[(x.name,x.get('class'),x.get_text(' ',strip=True)[:200]) for x in soup.select('[class*="char"],[class*="prop"],[class*="spec"],[class*="param"],[class*="feature"]')][:16]})
            except Exception as exc:evidence['pages'].append({'kind':kind,'url':url,'error':type(exc).__name__,'detail':str(exc)[:200]})
    finally:client.close()
    (OUT/(source+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(source,[(x.get('kind'),x.get('status'),x.get('count'),x.get('error')) for x in evidence['pages']],flush=True)
    return evidence

if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(audit,SOURCES))
    (OUT/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
