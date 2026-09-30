"""Manufacturer-scoped PS2/VB2 decoding from the published 2015+ notation.

Pure, offline enrichment. It never changes the collected product document.
Only complete understood designations receive the documented omitted defaults.
"""
from __future__ import annotations

import re
import unicodedata

SOURCE_URL='https://mega-k.com/news/notation_ps'
VERSION='megak-ps-vb-2026-09-30-v1'

# output, function, number of conductors (not connector contacts)
CIRCUITS={
    '1':('PNP','NO',3.), '13':('PNP','NO/NC',3.),
    '2':('NPN','NO',3.), '21':('NPN/PNP','NO',3.), '24':('NPN','NO/NC',3.),
    '3':('PNP','NC',3.), '34':('NPN/PNP','NC',3.), '4':('NPN','NC',3.),
    '5':('PNP','changeover',4.), '6':('NPN','changeover',4.), '56':('NPN/PNP','changeover',4.),
    '7':('2-wire','NO',2.), '8':('2-wire','NC',2.),
    '9':('analog-current',None,None),
    # The document leaves relay vs reed unspecified: do not choose one.
    '10':(None,'NO',None), '11':(None,'NC',None), '12':(None,'changeover',None),
}
TEMPERATURES={'T1':(-40.,105.),'T2':(-25.,105.),'T3':(-45.,85.),'T4':(-25.,120.)}
LOADS={'Y':400.,'Y05':500.,'Y2':2000.}
CONNECTORS={'C3':('m8',3.),'C4':('m12',4.),'C18':('m12',4.),'C19':('m12',4.),
            'C20':('m12',4.),'C21':('m12',4.),'C27':('m12',3.),'C29':('m12',3.)}
BODY_CODES={'32','33','34','38','39'}


def designation(value):
    value=unicodedata.normalize('NFKC',str(value or '')).upper()
    value=value.translate(str.maketrans('АВСЕКМНОРТХУ','ABCEKMHOPTXY')).translate(str.maketrans('–—−','---'))
    return re.sub(r'\s+','',value).replace(',', '.')


def is_megak(record):
    brand=str(record.get('manufacturer') or record.get('brand') or '').upper()
    if brand:return re.sub(r'[^A-ZА-Я0-9]','',brand) in ('МЕГАК','MEGAK')
    return record.get('source')=='megak'


def decode(model):
    code=designation(model)
    result={'model':str(model or ''),'normalized_model':code,'version':VERSION,'source_url':SOURCE_URL,
            'supported':False,'complete':False,'family':None,'fields':{},'special':[],
            'notes':[],'extras':{},'requires_review':False}
    def issue(text):
        result['notes'].append(text);result['requires_review']=True
    def add(name,value,token,section,default=False):
        result['fields'][name]={'value':value,'token':token,'section':section,'default':default}
    def positive(text):
        return float(text)>0
    if len(code)>160 or not re.match(r'^(PS|VB)2(?:[A-Z0-9]*)-',code):
        issue('Для обозначения не подтверждена новая система индуктивных датчиков PS2/VB2. Старые и другие серии не расшифровываются этим модулем.')
        return result
    result['supported']=True;result['family']='inductive'
    parts=code.split('-')
    if len(parts)<3 or len(parts)>5 or any(not p for p in parts):
        issue('Неполное обозначение или лишние разделители; значения по умолчанию не применены.')
        return result
    understood=True
    header=re.fullmatch(r'(PS|VB)2(?P<app>[AMDR]?)(?P<adjust>P?)(?P<timing>R1T4|R1T|R1|RT|R|T1|T)?(?P<level>Y?)(?P<ip>G?)',parts[0])
    if not header:
        understood=False;issue('Не распознаны специальные опции в первой группе обозначения.')
    else:
        h=header.groupdict();suffix=parts[0][3:]
        if 'D' in suffix:result['special'].append('analog')
        if 'R' in suffix:result['special'].append('speed')
        if h['app'] in ('A','M') or h['adjust'] or h['level'] or ('T' in suffix and 'R' not in suffix):
            issue('Обозначено специальное применение, регулировка или задержка: требуется проверка этих условий.')
        if h['ip']:add('ip',('68',),'G','1.8')
        result['extras']['Серия']=header[1]
        if suffix:result['extras']['Специальные опции']=suffix
    body=re.fullmatch(r'(?P<diameter>\d+(?:\.\d+)?)(?P<thread>[MGD])(?P<material>[AS]?)(?P<length>\d+(?:\.\d+)?)',parts[1])
    if body and positive(body['diameter']) and positive(body['length']):
        add('diameter',float(body['diameter']),parts[1],'2.1')
        add('body_type',{'M':'threaded','G':'pipe-threaded','D':'smooth'}[body['thread']],parts[1],'2.2')
        add('length',float(body['length']),parts[1],'2.5')
        if body['material']:add('material',{'A':'aluminium','S':'steel'}[body['material']],body['material'],'2.4')
    elif re.fullmatch(r'\d{2}',parts[1]) and 31<=int(parts[1])<=70:
        # A housing number is not a diameter or length (33 also has variants).
        if parts[1] in BODY_CODES:add('body_type','rectangular',parts[1],'2.3')
        else:issue('Номер специального корпуса распознан; его геометрия требует карточки или чертежа.')
        result['extras']['Номер корпуса']=parts[1]
    else:
        body=None;understood=False;issue('Группа корпуса не распознана однозначно; диаметр, длина и материал не угадываются.')
    electrical=re.fullmatch(r'(?P<sn>\d+(?:\.\d+)?)(?P<mount>[BN])(?P<circuit>\d{1,2})(?P<supply>[1-5])',parts[2])
    if not electrical or electrical['circuit'] not in CIRCUITS or not positive(electrical['sn']):
        understood=False;issue('Электрическая группа не разделяется однозначно на Sn, монтаж, схему и питание.')
    else:
        e=electrical.groupdict()
        add('sn',float(e['sn']),e['sn'],'3.2')
        add('mount',{'B':'flush','N':'non-flush'}[e['mount']],e['mount'],'3.3')
        for name,value in zip(('output','function','wire_count'),CIRCUITS[e['circuit']]):
            if value is not None:add(name,value,e['circuit'],'3.4')
        if e['circuit']=='9':result['special'].append('analog')
        voltage={'1':('DC',10.,30.),'2':('AC',35.,250.)}.get(e['supply'])
        if voltage:
            for name,value in zip(('voltage_type','vmin','vmax'),voltage):add(name,value,e['supply'],'3.5')
        elif e['supply']=='4':
            add('voltage_type','AC/DC','4','3.5')
            result['extras']['Питание по обозначению']='DC: 30…250 В; AC: 24…250 В'
            issue('Код питания 4 содержит разные диапазоны AC и DC. Общий диапазон из них не составляется.')
        else:
            issue('Код питания '+e['supply']+' требует карточки или паспорта; рабочий диапазон не подставлен.')
    connection=parts[3] if len(parts)>3 else ''
    modification=parts[4] if len(parts)>4 else ''
    # With no connection group, a complete T1/Y05/N suffix may follow group 3.
    modifier_pattern=r'(?P<temperature>T[1-4])?(?P<load>Y05|Y2|Y)?(?P<protection>N1|N)?'
    if len(parts)==4 and connection and re.fullmatch(modifier_pattern,connection):
        modification=connection;connection=''
    if connection in ('B','T'):
        add('connection','terminals',connection,'4.4')
    elif connection:
        conn=re.fullmatch(r'(?P<cable>KSI|ZSI|KPU|ZPU|PU|K|Z)?(?P<length>\d+)?(?P<plug>C29|C27|C21|C20|C19|C18|C4|C3)?',connection)
        if not conn or (not conn['cable'] and not conn['plug']) or (conn['length'] and not conn['cable']) or (conn['length'] and not positive(conn['length'])):
            understood=False;issue('Не распознан способ подключения; неизвестный код разъёма не заменяется типовым.')
        else:
            cable=bool(conn['cable']);plug=conn['plug']
            add('connection','cable+connector' if cable and plug else 'cable' if cable else 'connector',connection,'4')
            if plug:
                for name,value in zip(('connector','pin_count'),CONNECTORS[plug]):add(name,value,plug,'4.4')
            if cable:
                result['extras']['Материал кабеля']='Силикон' if 'SI' in conn['cable'] else 'Полиуретан' if 'PU' in conn['cable'] else 'ПВХ (по умолчанию)'
                result['extras']['Длина кабеля, м']=float(conn['length'])/10 if conn['length'] else '2 (по умолчанию)'
    mods=re.fullmatch(modifier_pattern,modification)
    if not mods:
        understood=False;issue('Неизвестная модификация; стандартные температура, ток и IP не подставлены.')
    else:
        if mods['temperature']:
            for name,value in zip(('tmin','tmax'),TEMPERATURES[mods['temperature']]):add(name,value,mods['temperature'],'5.1')
        if mods['load']:add('load',LOADS[mods['load']],mods['load'],'5.2')
        if mods['protection']:result['extras']['Код защиты выхода']=mods['protection']
    result['complete']=understood
    if understood:
        if 'ip' not in result['fields']:add('ip',('67',),'без G','1.8',True)
        if body and not body['material']:add('material','brass','без A/S','2.4',True)
        if not mods['temperature']:
            add('tmin',-25.,'без T1…T4','5.1',True);add('tmax',75.,'без T1…T4','5.1',True)
        if not mods['load']:add('load',250.,'без Y','5.2',True)
    else:
        # Omitted defaults in additional cable data are unsafe in a partial code.
        result['extras']={k:v for k,v in result['extras'].items() if 'по умолчанию' not in str(v)}
    result['special']=sorted(set(result['special']))
    return result


def enrich(sensor,record):
    """Fill only gaps. Keep card values and expose contradictions separately."""
    if not is_megak(record):return sensor
    decoded=decode(sensor.model);sensor.decoding=decoded
    if not decoded['supported']:return sensor
    if sensor.family is None:sensor.family=decoded['family']
    elif sensor.family!=decoded['family']:
        decoded['requires_review']=True
        decoded['notes'].append('Тип изделия в карточке противоречит обозначению PS2/VB2; дополнение характеристик остановлено.')
        return sensor
    sensor.special=tuple(sorted(set(sensor.special)|set(decoded['special'])))
    for name,entry in decoded['fields'].items():
        existing=sensor.values.get(name);value=entry['value']
        entry['card_value']=existing
        if name in sensor.conflicts:entry['application']='card_conflict'
        elif existing is None:
            sensor.values[name]=value;entry['application']='filled'
            sensor.raw.setdefault(name,[]).append({'field':'Обозначение МЕГА-К','value':sensor.model,
                'token':entry['token'],'section':entry['section'],'source_url':SOURCE_URL,'version':VERSION,'default':entry['default']})
        elif existing==value:entry['application']='confirmed'
        elif entry['default'] or (name=='material' and value=='steel' and existing=='stainless'):
            entry['application']='card_priority'
        else:
            entry['application']='conflict';decoded['requires_review']=True
    return sensor


def field_source(sensor,name):
    row=(sensor.decoding or {}).get('fields',{}).get(name,{})
    if row.get('application')=='filled':
        return f'Обозначение МЕГА-К: {row["token"]}, п. {row["section"]}'+(' (значение по умолчанию)' if row['default'] else '')
    if name in sensor.conflicts:return 'Противоречивые данные карточки'
    if row.get('application')=='conflict':return 'Карточка; противоречие с обозначением'
    if sensor.values.get(name) is not None:return 'Карточка / справочник'
    return 'Нет подтверждённых данных'
