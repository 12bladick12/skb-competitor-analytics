"""Conservative parsers: document body mentions do not establish party roles."""
from datetime import date
import hashlib
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .model import case_key, digest, Page

MONTHS={'января':1,'февраля':2,'марта':3,'апреля':4,'мая':5,'июня':6,
        'июля':7,'августа':8,'сентября':9,'октября':10,'ноября':11,'декабря':12}
BLOCKS=('подтвердите, что вы не робот','введите код с картинки','подтверждение действия',
        'проверка безопасности','access denied','forbidden','доступ заблокирован',
        'доступ к сервису ограничен','just a moment')


def blocked(body, status=200):
    if status in (401,403,451):return 'access_denied'
    if status==429:return 'rate_limited'
    if any(x in body.casefold() for x in BLOCKS):return 'challenge'
    if status>=400:return 'error'
    return ''


def total(body, supreme=False):
    pattern=(r'Найдено:[ \t\u00a0]*([0-9][0-9 \t\u00a0]*)' if supreme else
             r'Найден[оы]?[ \t\u00a0]*:?[ \t\u00a0]*([0-9][0-9 \t\u00a0]*)\s+документ')
    match=re.search(pattern,body,re.I)
    if match:return int(re.sub(r'\s','',match[1]))
    if not supreme and 'Документы не найдены' in body:return 0
    return None


def sudact_links(html, base, section):
    soup=BeautifulSoup(html,'html.parser')
    docs={};next_url=None
    for a in soup.select('a[href]'):
        url=urljoin(base,a['href']);p=urlsplit(url)
        if p.hostname!='sudact.ru' or p.scheme!='https':continue
        if re.fullmatch('/'+re.escape(section)+r'/doc/[A-Za-z0-9]+/',p.path):
            docs.setdefault('https://sudact.ru'+p.path,a.get_text(' ',strip=True))
        if a.get_text(' ',strip=True).casefold()=='следующая':next_url=url
    return [{'url':url,'title':title} for url,title in docs.items()],next_url


def heading_content(soup, title):
    heading=next((h for h in soup.select('h3') if h.get_text(' ',strip=True)==title),None)
    if heading is None:return ''
    parts=[]
    for node in heading.next_siblings:
        if getattr(node,'name',None)=='h3':break
        parts.append(str(node))
    return BeautifulSoup(''.join(parts),'html.parser').get_text(' ',strip=True)


def sudact_document(html, url, entity, domain='arbitral'):
    source='sudact_'+{'arbitral':'arbitral','general':'regular','magistrate':'magistrate'}[domain]
    result=Page(source,domain,entity['id'],url,reason='Опубликованная копия; полнота и текущее состояние дела не подтверждены.')
    raw=html.encode();proof=hashlib.sha256(raw).hexdigest();result.objects[proof]=raw
    soup=BeautifulSoup(html,'html.parser')
    title=soup.h1.get_text(' ',strip=True) if soup.h1 else ''
    number=re.search(r'(?:делу|дела)\s*№\s*([^\s<]+)',title,re.I)
    parties=[]
    for label,role in [('Истцы:','plaintiff'),('Ответчики:','defendant'),('Заявители:','applicant'),('Третьи лица:','third_party')]:
        content=heading_content(soup,label)
        if re.search(r'(?<!\d)'+entity['inn']+r'(?!\d)',content):
            parties.append({'inn':entity['inn'],'name':entity['name'],'role':role,
                            'identity_evidence':entity['inn'],'role_evidence':label+' '+content,
                            'source_url':url,'evidence_ids':[proof]})
    candidate={'id':digest([entity['id'],url]),'entity_id':entity['id'],'url':url,'title':title,
               'reason':'Участие не подтверждено структурированным списком сторон.', 'evidence_ids':[proof]}
    if not number or not parties:
        result.candidates.append(candidate);return result
    case_number=number[1].replace('A','А').rstrip('.,;')
    court_node=soup.select_one('.b-justice')
    court=court_node.get_text(' ',strip=True) if court_node else heading_content(soup,'Суд:')
    if not court:
        result.candidates.append(candidate);return result
    origin=re.match(r'А\d+',case_number)
    identity_court=origin[0] if domain=='arbitral' and origin else court
    ident=case_key(domain,identity_court,case_number)
    case={'id':ident,'number':case_number,'jurisdiction':domain,'court':court,'identity_court':identity_court,
          'participants':parties,'entity_ids':[entity['id']],'url':url,'official_url':None,
          'status':None,'subject':None,'registered_on':None,'next_hearing':None,
          'judge':heading_content(soup,'Судьи дела:') or None,'claim_amount':None,'award_amount':None,
          'costs':None,'linked_cases':[], 'evidence_ids':[proof], 'source':source,
          'as_of':None,'limitations':'Копия судебного акта. Актуальное состояние дела не проверено.'}
    result.cases.append(case)
    m=re.search(r'от (\d{1,2}) ([а-я]+) (\d{4})',title,re.I)
    occurred=None
    if m and m[2].lower() in MONTHS:
        occurred=date(int(m[3]),MONTHS[m[2].lower()],int(m[1])).isoformat()
    # Keep the act text, not navigation/ads, in the document. No amount/outcome inference.
    area=soup.select_one('.h-col1-inner3') or soup
    for node in area.select('script,style,.footer_adv,.bookmarkBlockContainer'):node.decompose()
    act=area.get_text(' ',strip=True)
    document={'id':digest(url),'case_id':ident,'entity_ids':[entity['id']],'title':title,'date':occurred,
              'court':court,'url':url,'evidence_ids':[proof],'source':source,'text':act}
    result.documents.append(document)
    result.events.append({'canonical_event_id':digest([ident,court,title]),'case_id':ident,
                          'type':'judicial_act','title':title,'description':title+'; '+court,
                          'occurred_on':occurred,'document_id':document['id'],'url':url,
                          'evidence_ids':[proof],'date_evidence':'Заголовок опубликованного судебного акта'})
    return result
