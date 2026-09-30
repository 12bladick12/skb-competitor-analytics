"""Typed sensor characteristics with explicit missing/conflicting values.

Names and units are normalized. Only a manufacturer-specific documented decoder
may supply absent values; price never enters normalization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import unicodedata

from .megak_notation import is_megak
from .notations import enrich


def key(value):
    text=unicodedata.normalize('NFKC',str(value or '')).casefold().replace('ё','е')
    return re.sub(r'\s+',' ',text).strip().rstrip(':').strip()


def model_key(value):
    return re.sub(r'\s+','',str(value or '').upper().replace('Ё','Е').translate(str.maketrans('–—−','---')))


def number(value):
    if value is None:return None
    text=str(value).replace('\xa0',' ').replace('−','-')
    values=re.findall(r'(?<![\d.])[-+]?\d+(?:[.,]\d+)?',text)
    if len(values)!=1:return None
    result=float(values[0].replace(',','.'))
    return result if math.isfinite(result) else None


def interval(value):
    if value is None:return None
    text=str(value).replace('−','-').replace('–','-').replace('—','-')
    # A minus between two positive voltage values is a separator; a negative
    # lower temperature bound remains signed. No range is inferred from a nominal.
    text=re.sub(r'(?<=\d)\s*-\s*(?=\d)',' .. ',text)
    vals=re.findall(r'[-+]?\d+(?:[.,]\d+)?',text)
    if len(vals)!=2:return None
    lo,hi=(float(v.replace(',','.')) for v in vals)
    return (lo,hi) if math.isfinite(lo) and math.isfinite(hi) and lo<=hi else None


def output(value):
    text=key(value).upper()
    found=[v for v in ('PNP','NPN','NAMUR') if re.search(r'\b'+v+r'\b',text)]
    if len(found)==1:return found[0]
    if len(found)>1:return '/'.join(sorted(found))
    if re.search(r'2\s*[-хx]?\s*провод',text):return '2-wire'
    if text.startswith(('AC','DC')):return '2-wire'
    if 'реле' in text:return 'relay'
    return None


def switching(value):
    text=key(value).upper()
    text=re.sub(r'\bНО\b','NO',text);text=re.sub(r'\bНЗ\b','NC',text)
    no=bool(re.search(r'\bNO\b',text) or 'НОРМАЛЬНО РАЗОМКНУТ' in text or re.search(r'(?<!РА)ЗАМЫКАЮЩ',text))
    nc=bool(re.search(r'\bNC\b',text) or 'НОРМАЛЬНО ЗАМКНУТ' in text or 'РАЗМЫКАЮЩ' in text)
    if no and nc:return 'NO/NC'
    if no:return 'NO'
    if nc:return 'NC'
    # A configurable output or relay changeover needs its own wiring check.
    if 'ПЕРЕКЛЮЧАЮЩ' in text:return 'changeover'
    return None


def mounting(value):
    text=key(value)
    if 'quasi' in text or 'квази' in text:return 'quasi-flush'
    if text in ('non-flush','non flush','unshielded'):return 'non-flush'
    if text in ('flush','shielded'):return 'flush'
    if 'невстраив' in text.replace(' ','') or 'не заподлицо' in text or 'незаподлицо' in text:return 'non-flush'
    if 'встраив' in text or text=='заподлицо':return 'flush'
    return None


def material(value):
    text=key(value)
    if 'brass' in text:return 'brass'
    if 'stainless' in text:return 'stainless'
    if 'латун' in text:return 'brass'
    if 'нержав' in text or 'нерж.' in text:return 'stainless'
    if any(v in text for v in ('пласт','полимер','текаформ','полиамид','pbt','abs')):return 'plastic'
    if any(v in text for v in ('алюмин','д16','дюрал')):return 'aluminium'
    if 'сталь' in text:return 'steel'
    return text or None


def connection(value):
    text=key(value)
    cable='кабел' in text or 'провод' in text or 'cable' in text
    plug='разъем' in text or 'штек' in text or 'connector' in text or bool(re.search(r'\b[mм](8|12|16|23)\b',text))
    if cable and plug:return 'cable+connector'
    if cable:return 'cable'
    if plug:return 'connector'
    if 'клем' in text:return 'terminals'
    return None


def supply_type(value):
    text=key(value).upper()
    dc='DC' in text or 'ПОСТОЯНН' in text
    ac='AC' in text or 'ПЕРЕМЕНН' in text
    return 'AC/DC' if ac and dc else 'AC' if ac else 'DC' if dc else None


def body_type(value):
    text=key(value)
    if 'резьб' in text or re.search(r'\b[mм]\s*\d',text):return 'threaded'
    if 'гладк' in text:return 'smooth'
    if 'прямоугол' in text or 'кубич' in text:return 'rectangular'
    if 'щелев' in text:return 'slot'
    return None


def ip(value):
    values=sorted(set(re.findall(r'IP\s*(\d\d(?:K|[A-D])?)',str(value).upper())))
    return tuple(values) if values else None


def connector(value):
    text=key(value)
    # Normalize visually identical Cyrillic/Latin M; retain any thread pitch.
    return re.sub(r'^[мm](?=\d)','m',text) or None


@dataclass
class Sensor:
    id: str
    model: str
    category: str=''
    family: str|None=None
    special: tuple[str,...]=()
    values: dict=field(default_factory=dict)
    raw: dict=field(default_factory=dict)
    conflicts: set=field(default_factory=set)
    profile: str='general'
    source_row: int|None=None
    article: str=''
    decoding: dict=field(default_factory=dict)


# The list is also consumed by the bundled-catalog builder, preserving provenance.
ALIASES={
    'body':('Типоразмер корпуса, мм','Размер резьбы корпуса','Размер корпуса, ДxШxДл','Диаметр резьбового корпуса','Диаметр резьбового корпуса, мм','Размер корпуса','Типоразмер','Корпус','Тип корпуса'),
    'length':('Длина цилиндрического корпуса, мм','Длина корпуса, мм','Длина в мм.'),
    'sn':('Номинальное расстояние переключения, Sn, мм','Номинальное расстояние срабатывания','Расстояние срабатывания Sn, мм','Номинальное расстояние срабатывания [Sn]','Расстояние срабатывания, мм','Расстояние срабатывания номинальное (Sn)'),
    'output':('Схема выхода','Структура выхода','Тип контакта / Структура выхода','Тип выходного сигнала','Схема подключения','Тип выхода'),
    'function':('Функция выхода','Функция переключения','Тип контакта / Структура выхода','Тип коммутации'),
    'mount':('Способ установки','Монтаж','Тип монтажа','Способ монтажа','Монтажное исполнение'),
    'material':('Материал корпуса датчика','Материал корпуса'),
    'connection':('Способ подключения','Соединение','Электрическое подключение','Присоединение / Подключение'),
    'ip':('Степень защиты IP','Степень защиты','Степень защиты корпуса','Степень защиты по ГОСТ 14254-96','Степень защиты по ГОСТ 14254-2015','Степень защиты по IEC 60529'),
    'temperature':('Рабочая температура окружающей среды, °С','Диапазон рабочих температур','Температура эксплуатации, °C','Температура эксплуатации','Рабочая температура'),
    'tmin':('Температура эксплуатации Min, °C','Минимальная рабочая температура, °C','Минимальная рабочая температура, °С'),
    'tmax':('Температура эксплуатации Max, °C','Максимальная рабочая температура, °C','Максимальная рабочая температура, °С'),
    'voltage':('Диапазон питающих напряжений, В','Диапазон рабочих напряжений, Uраб.','Напряжение питания, В','Диапазон питающего напряжения','Напряжение питания рабочее'),
    'voltage_type':('Тип напряжения питания','Питание'),
    'load':('Ток нагрузки, мА, не более','Максимальный рабочий ток, Imax','Максимальный рабочий ток, мА','Макс. ток нагрузки, А','Ток нагрузки максимальный (Ie)','Коммутируемый ток [DC]'),
    'frequency':('Максимальная частота переключения, Гц','Частота переключения, Fmax','Частота переключения, Гц','Максимальная частота переключения','Частота переключения максимальная (f)'),
    'wire_count':('Количество проводов (pin)','Кол-во проводов','Количество проводов'),
    'connector':('Тип разъёма','Тип разъема','Резьба разъема','Тип разъёма (2-х проводные на постоянное напряжение 15-30В)'),
    'pin_count':('Количество контактов','Число контактов'),
    'special_pressure':('Рабочее давление','Рабочее давление, бар','Максимальное рабочее давление, бар','Давление, бар'),
    'special_analog':('Аналоговый выход по напряжению, В','Аналоговый выход по току, мА'),
    'special_speed':('Диапазон измерения частоты, Гц','Диапазон частоты воздействия, fo, Гц'),
    'special_ex':('Маркировка взрывозащиты','Взрывозащита'),
}
for _name, _extra in {
    'body':('Размер цилиндрического корпуса, мм','Резьба корпуса','Конструкция корпуса'),
    'sn':('Расстояние переключения, мм','Расстояние срабатывания','Sensing distance','Rated distance [Sn]'),
    'output':('Тип выхода/функция','Output type','Выходной сигнал'),
    'function':('Тип выхода/функция','Output function','Выходной сигнал','Тип выходного контакта'),
    'mount':('Установка','Installation','Mounting'),
    'voltage':('Рабочее напряжение, В','Рабочее напряжение, Uраб','Напряжение питания, Uраб.','Supply voltage','Operating voltage'),
    'pin_count':('Число контактов, pin','Количество контактов, pin'),
    'frequency':('Частота переключения max, Гц','Рабочая частота, Гц','Switching frequency'),
    'connection':('Connection type','Connection'),
    'material':('Housing material','Обозначение материала корпуса'),
    'ip':('Degree of protection','Protection structure'),
    'temperature':('Ambient temperature',),
}.items(): ALIASES[_name] += _extra
ALIASES['diameter']=('Диаметр корпуса, мм','Диаметр цилиндрического корпуса')
ALIAS_KEYS={name:tuple(key(alias) for alias in aliases) for name,aliases in ALIASES.items()}
PREFIXES={'length':'_Длина корпуса','diameter':'_Диаметр цилиндрического корпуса','tmin':'_Рабочая температура окружающей среды мин.',
          'tmax':'_Рабочая температура окружающей среды макс.','body_type':'_Тип корпуса'}
SPECIAL_LABELS={'speed':'Контроль минимальной скорости','pressure':'Высокое давление','namur':'NAMUR','slot':'Щелевые','ex':'Взрывозащищённые','analog':'Аналоговый выход'}


def normalize_sensor(record):
    """Accept either our catalog record or a collected product + _specifications."""
    details=record.get('_specifications') or {}
    raw_attrs=record.get('props') or details.get('attributes') or []
    if isinstance(raw_attrs,dict):entries=list(raw_attrs.items())
    else:entries=[(a.get('name',''),a.get('value')) for a in raw_attrs if isinstance(a,dict)]
    props={}
    for name,value in entries:
        if value not in (None,'') and key(value) not in ('-','—','не указано','нет данных','н/д','n/a'):
            props.setdefault(key(name),[]).append((str(name),str(value)))
    category=str(record.get('category') or details.get('category') or '')
    model=str(record.get('model') or record.get('article') or '')
    sensor=Sensor(str(record.get('code') or record.get('rule_id') or model),model,category,source_row=record.get('row'),article=str(record.get('article') or ''))
    def add(name,value,source,raw):
        if value is None:return
        sensor.raw.setdefault(name,[]).append({'field':source,'value':raw})
        if name in sensor.conflicts:return
        if name in sensor.values and sensor.values[name]!=value:
            sensor.values.pop(name,None);sensor.conflicts.add(name)
        else:sensor.values[name]=value
    def values(name):
        for alias in ALIAS_KEYS.get(name,()):
            yield from props.get(alias,[])
        if name in PREFIXES:
            for n,vals in props.items():
                if n.startswith(key(PREFIXES[name])):yield from vals
    for name,parser in [('length',number),('sn',number),('output',output),('function',switching),('mount',mounting),
                        ('material',material),('connection',connection),('ip',ip),('tmin',number),('tmax',number),
                        ('wire_count',number),('pin_count',number),('connector',connector),('diameter',number),('body_type',body_type),('voltage_type',supply_type)]:
        for field_name,raw in values(name):add(name,parser(raw),field_name,raw)
    if is_megak(record):
        for field_name,raw in props.get(key('Длина'),[]):
            if re.search(r'\b(?:мм|mm)\b',raw,re.I):add('length',number(raw),field_name,raw)
    for field_name,raw in values('body'):
        add('body_type',body_type(raw),field_name,raw)
        m=re.search(r'[MМmм]\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×XХ]\s*(\d+(?:[.,]\d+)?))?',raw)
        if m:
            add('diameter',float(m[1].replace(',','.')),field_name,raw)
            if m[2]:add('pitch',float(m[2].replace(',','.')),field_name,raw)
        elif key(field_name) in {key('Размер корпуса'),key('Размер цилиндрического корпуса, мм')}:
            add('diameter',number(raw),field_name,raw)
    for field_name,raw in values('temperature'):
        bounds=interval(raw)
        if bounds:add('tmin',bounds[0],field_name,raw);add('tmax',bounds[1],field_name,raw)
    for field_name,raw in values('voltage'):
        bounds=interval(raw)
        if bounds:add('vmin',bounds[0],field_name,raw);add('vmax',bounds[1],field_name,raw)
        add('voltage_type',supply_type(raw),field_name,raw)
    for name in ('load','frequency'):
        for field_name,raw in values(name):
            value=number(raw)
            if value is not None:
                if name=='load' and (key(field_name).endswith(', а') or re.search(r'\d\s*[АA]\b',raw)):value*=1000
                if name=='frequency' and ('кгц' in key(raw) or 'khz' in key(raw)):value*=1000
            add(name,value,field_name,raw)
    purpose=' '.join(str(v) for n,v in entries if any(w in key(n) for w in ('специаль','исполнение','дополнитель','функция')))
    text=key(' '.join([category,str(record.get('title','')),str(details.get('description','')),purpose]))
    family_text=key(category+' '+str(record.get('title',''))+' '+str(details.get('category','')))
    def family(value):
        found=list(dict.fromkeys(code for stem,code in (('индуктив','inductive'),('inductive','inductive'),('емкост','capacitive'),('capacitive','capacitive'),('геркон','reed'),('магниточувств','reed'),('оптичес','optical'),('photoelectric','optical'),('ультразвук','ultrasonic')) if stem in key(value)))
        if not found:
            found=[code for stem,code in (('датчик давления','pressure'),('датчики давления','pressure'),('датчик температуры','temperature'),('датчики температуры','temperature'),('датчик уровня','level'),('датчики уровня','level')) if stem in key(value)]
        return found[0] if len(found)==1 else None
    sensor.family=family(family_text)
    # An explicit type attribute is stronger than a broad category.
    for alias in ('Тип датчика','Принцип действия','Принцип работы'):
        for _,v in props.get(key(alias),[]):
            explicit=family(v)
            if explicit:sensor.family=explicit
    flags=[]
    if 'контроля скорости' in text or 'контроля минимальной скорости' in text:flags.append('speed')
    if 'высокого давления' in text or 'высокое давление' in text:flags.append('pressure')
    if 'namur' in text or sensor.values.get('output')=='NAMUR':flags.append('namur')
    if 'щелев' in text or sensor.values.get('body_type')=='slot':flags.append('slot')
    if 'взрывозащи' in text or re.search(r'\bex\s*[miad]',text) or 'exm' in model.casefold():flags.append('ex')
    if 'аналогов' in text:flags.append('analog')
    # Dedicated properties also identify special execution, even when its name
    # is absent from the broad catalog category. Placeholder values do not.
    for attr,flag in (('special_pressure','pressure'),('special_analog','analog'),('special_speed','speed'),('special_ex','ex')):
        if any(key(v) not in ('-','—','нет','no','не применяется','0','') for _,v in values(attr)):
            flags.append(flag)
    sensor.special=tuple(sorted(set(flags)))
    if 'низких температур' in text or 'низкотемператур' in text:sensor.profile='cold'
    elif 'высоких температур' in text or 'высокотемператур' in text:sensor.profile='hot'
    return enrich(sensor,record)
