"""Directional, explainable matching. No learned weights or price-based ranking."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import json
import gzip
import math

from .matching_normalize import Sensor, normalize_sensor, model_key, SPECIAL_LABELS
from .notations import field_source

VERSION='inductive-2026-10-01-v6'
STATUS_LABELS={'direct':'Прямой аналог','close':'Близкий аналог','review':'Требует проверки','incompatible':'Не подходит','unsupported':'Алгоритм ещё не добавлен'}
FIELD_LABELS={'body_type':'Форма корпуса','diameter':'Диаметр корпуса, мм','pitch':'Шаг резьбы, мм','output':'Схема выхода',
              'function':'Функция выхода','voltage_type':'Тип питания','vmin':'Минимальное питание, В','vmax':'Максимальное питание, В',
              'mount':'Монтаж','sn':'Расстояние срабатывания Sn, мм','tmin':'Нижняя температура, °C','tmax':'Верхняя температура, °C',
              'material':'Материал корпуса (группа)','connection':'Способ подключения','ip':'Защита IP','load':'Максимальный ток нагрузки, мА',
              'frequency':'Частота переключения, Гц','length':'Длина корпуса, мм','wire_count':'Число проводов','connector':'Разъём','pin_count':'Число контактов'}
VALUE_LABELS={'threaded':'Цилиндрический резьбовой','smooth':'Цилиндрический гладкий','rectangular':'Прямоугольный','slot':'Щелевой',
              'quasi-flush':'Квазизаподлицо (quasi-flush)','configurable':'Программируемый NO/NC',
              'pipe-threaded':'Цилиндрический с трубной резьбой','analog-current':'Аналоговый токовый выход',
              'brass':'Латунь','stainless':'Нержавеющая сталь','plastic':'Пластик','aluminium':'Алюминиевый сплав','steel':'Сталь',
              'flush':'Встраиваемый','non-flush':'Невстраиваемый','cable':'Кабель','connector':'Разъём','cable+connector':'Кабель с разъёмом',
              'terminals':'Клеммник','2-wire':'2-проводный','relay':'Реле','changeover':'Переключающий'}
OUTCOME_LABELS={'exact':'Совпадает','reserve':'Соответствует с запасом','difference':'Отличие с оговоркой','secondary':'Второстепенное отличие',
                'missing':'Недостаточно данных','incompatible':'Несовместимо'}
MANDATORY=('body_type','diameter','output','function','voltage_type','vmin','vmax','mount')
IMPORTANT=('sn','tmin','tmax','material','connection','ip','load','frequency')


def display(value,attribute=''):
    if value is None:return 'Не указано'
    if attribute=='ip':return ' / '.join('IP'+x for x in value)
    if isinstance(value,float):return f'{value:g}'.replace('.',',')
    return VALUE_LABELS.get(value,str(value))


@dataclass
class Match:
    candidate: Sensor
    status: str
    fields: list[dict]
    profile: str
    notices: list[str]=field(default_factory=list)
    priority: int=0
    temperature_loss: float=0
    risk: tuple=()
    length_gap: float=math.inf
    reserves: int=0

    @property
    def differences(self):return [x for x in self.fields if x['outcome'] in ('difference','secondary','incompatible')]

    @property
    def missing(self):return [x for x in self.fields if x['outcome']=='missing' and x['group']!='Второстепенные']

    def rows(self):
        return [{'Группа':x['group'],'Характеристика':FIELD_LABELS[x['key']],
                 'Конкурент':display(x['reference'],x['key']),'Наша модель':display(x['candidate'],x['key']),
                 'Оценка':OUTCOME_LABELS[x['outcome']],'Источник конкурента':x.get('reference_source',''),
                 'Пояснение':x['reason']} for x in self.fields]


def evaluate(reference:Sensor,candidate:Sensor,profile='auto',max_length=None):
    profile=reference.profile if profile=='auto' else profile
    if profile not in ('general','cold','hot'):raise ValueError('Неизвестное назначение подбора')
    if max_length is not None and (not math.isfinite(max_length) or max_length<=0):raise ValueError('Длина должна быть положительным числом')
    fields=[];notices=[];forced_review=False;incompatible=False
    if reference.family not in (None,'inductive') or candidate.family!='inductive':
        return Match(candidate,'unsupported',[],profile,['Для этого типа продукции алгоритм ещё не добавлен.'])
    if reference.family is None:forced_review=True;notices.append('Тип датчика конкурента не подтверждён характеристиками.')
    if reference.decoding.get('supported') and reference.decoding.get('requires_review'):
        forced_review=True
        notices.append('Расшифровка производителя требует проверки: противоречие с карточкой, непроверенная редакция, неизвестная опция или особое исполнение. Подробности — в блоке расшифровки.')
    if reference.special!=candidate.special:
        incompatible=True;notices.append('Разные специальные исполнения: '+(', '.join(SPECIAL_LABELS[s] for s in reference.special) or 'стандартное')+' → '+(', '.join(SPECIAL_LABELS[s] for s in candidate.special) or 'стандартное')+'.')
    elif reference.special:
        forced_review=True;notices.append('Для исполнения «'+', '.join(SPECIAL_LABELS[s] for s in reference.special)+'» требуется отдельная проверка специальных параметров.')
    if reference.values.get('body_type') not in (None,'threaded','smooth'):
        forced_review=True;notices.append('Нужна проверка установочных размеров этого корпуса; диаметр не заменяет геометрию.')
    mandatory=list(MANDATORY)
    if reference.values.get('body_type') in (None,'threaded'):mandatory.insert(2,'pitch')
    for name in (*mandatory,*IMPORTANT,'length','wire_count','connector','pin_count'):
        if name in ('connector','pin_count') and reference.values.get('connection') not in ('connector','cable+connector'):continue
        r=reference.values.get(name);c=candidate.values.get(name)
        group='Обязательные' if name in mandatory else 'Важные' if name in IMPORTANT else 'Второстепенные'
        if (name=='tmax' and profile=='cold') or (name=='tmin' and profile=='hot'):group='Второстепенные'
        if name=='length' and max_length is not None:group='Обязательные'
        result='exact';reason=''
        if r is None or c is None:
            result='missing';reason='Конфликт значений в источнике.' if name in reference.conflicts or name in candidate.conflicts else 'Нет подтверждённого значения с обеих сторон.'
            if name=='pitch':reason+=' Шаг не выводится из одного диаметра.'
        elif r==c:pass
        elif name in ('vmin','vmax'):
            result='reserve' if (c<=r if name=='vmin' else c>=r) else 'incompatible'
            reason='Диапазон питания покрывает исходный.' if result=='reserve' else 'Диапазон питания не покрывает исходный; нужна проверка рабочего напряжения.'
        elif name=='voltage_type':
            result='reserve' if set(r.split('/'))<=set(c.split('/')) else 'incompatible'
            reason='Сохраняется требуемый род тока.' if result=='reserve' else 'Не подтверждена совместимость рода тока.'
        elif name in mandatory:result='incompatible';reason='Обязательные характеристики различаются.'
        elif name=='length':result='secondary';reason=f'Корпус {"длиннее" if c>r else "короче"} на {display(abs(c-r))} мм.'
        elif name=='sn':result='difference';reason=f'Отклонение {display(abs(c-r))} мм. Ближайшее значение в любую сторону; допуск прямого аналога не согласован.'
        elif name in ('tmin','tmax'):
            better=c<r if name=='tmin' else c>r
            result='reserve' if better else ('secondary' if group=='Второстепенные' else 'difference')
            reason=('Запас по значимой температурной границе.' if group=='Важные' else 'Граница второстепенна для выбранного назначения; отличие сохранено.') if better or group=='Второстепенные' else 'Потеря части исходного температурного диапазона.'
        elif name in ('load','frequency'):
            result='reserve' if c>r else 'difference';reason='Сравнение заявленных предельных значений в одинаковых единицах.'
        elif name=='ip':
            result='reserve' if set(r)<=set(c) else 'difference'
            reason='Сохранены все исходные виды защиты.' if result=='reserve' else 'Виды защиты различаются; IP67 само по себе не подтверждает IP65/IP66.'
        elif name=='material':result='difference';reason='Другой материал — близкий аналог; универсального преимущества нет.'
        elif name=='connection':result='difference';reason='Другой способ подключения — близкий аналог; учесть подключение на месте.'
        else:result='secondary';reason='Дополнительное отличие подключения; проверить схему.'
        if name in ('wire_count','connector','pin_count') and r is not None:
            # Confirmed incompatible wiring cannot be hidden by a shared PNP label.
            if result in ('secondary','missing'):forced_review=True;reason+=' Требуется проверка подключения.'
        if name=='length' and max_length is not None and c is not None and c>max_length:
            result='incompatible';reason=f'Длина превышает монтажное ограничение {display(max_length)} мм.'
        fields.append(dict(key=name,reference=r,candidate=c,group=group,outcome=result,reason=reason,reference_source=field_source(reference,name)))
    if incompatible or any(x['outcome']=='incompatible' for x in fields):status='incompatible'
    elif forced_review or any(x['outcome']=='missing' and x['group']!='Второстепенные' for x in fields):status='review'
    elif any(x['outcome']=='difference' for x in fields):status='close'
    else:status='direct'
    ref=reference.values;own=candidate.values
    def delta(name,direction=0):
        if ref.get(name) is None or own.get(name) is None:return math.inf
        gap=own[name]-ref[name]
        return abs(gap) if direction==0 else max(0,gap*direction)
    deficits=(delta('tmin',1),delta('tmax',-1))
    temp_loss=deficits[0] if profile=='cold' else deficits[1] if profile=='hot' else 0
    def difference(name):
        f=next(x for x in fields if x['key']==name)
        return math.inf if f['outcome']=='missing' else int(f['outcome']=='difference')
    # Material and connection are the agreed equal-priority tradeoff. Other
    # important criteria remain separate axes; there is no weighted sum.
    risk=(delta('sn'),difference('material')+difference('connection'),difference('ip'),delta('load',-1),delta('frequency',-1))
    if profile=='general':risk=deficits+risk
    return Match(candidate,status,fields,profile,notices,temperature_loss=temp_loss,risk=risk,length_gap=delta('length'),
                 reserves=sum(x['outcome']=='reserve' for x in fields))


def dominates(a:Match,b:Match):
    """Priority of the purpose first; incomparable compromises retain alternatives."""
    if a.temperature_loss!=b.temperature_loss:return a.temperature_loss<b.temperature_loss
    if all(x<=y for x,y in zip(a.risk,b.risk)) and any(x<y for x,y in zip(a.risk,b.risk)):return True
    if a.risk!=b.risk:return False
    if a.length_gap!=b.length_gap:return a.length_gap<b.length_gap
    return a.reserves<b.reserves


def rank(matches):
    ranked=[]
    for status in ('direct','close','review'):
        remaining=[m for m in matches if m.status==status];priority=1
        while remaining:
            front=[a for a in remaining if not any(dominates(b,a) for b in remaining if b is not a)]
            if not front:front=remaining[:]
            for m in front:m.priority=priority
            ranked.extend(sorted(front,key=lambda m:model_key(m.candidate.model)))
            taken={id(m) for m in front};remaining=[m for m in remaining if id(m) not in taken];priority+=1
    return ranked


class Matcher:
    def __init__(self,products):
        self.products=list(products)
        self.index=defaultdict(list)
        self.by_model=defaultdict(list)
        for p in self.products:
            self.index[(p.values.get('body_type'),p.values.get('diameter'))].append(p)
            self.by_model[model_key(p.model)].append(p)
            self.by_model[model_key(p.id)].append(p)
            if p.article:self.by_model[model_key(p.article)].append(p)

    def resolve(self,model):
        matches={p.id:p for p in self.by_model.get(model_key(model),[])}
        return next(iter(matches.values())) if len(matches)==1 else None

    def suggest(self,reference,profile='auto',max_length=None):
        if reference.family not in (None,'inductive'):return []
        if not any(reference.values.get(k) is not None for k in ('diameter','output','sn')):return []
        body=reference.values.get('body_type');diameter=reference.values.get('diameter')
        candidates=[p for (b,d),rows in self.index.items() if (body is None or b is None or b==body) and (diameter is None or d is None or d==diameter) for p in rows]
        matches=[evaluate(reference,p,profile,max_length) for p in candidates]
        return rank([m for m in matches if m.status not in ('incompatible','unsupported')])


@lru_cache(maxsize=1)
def load_catalog():
    path=Path(__file__).parent/'assets'/'our_inductive_catalog.json.gz'
    payload=json.loads(gzip.decompress(path.read_bytes()).decode('utf-8'))
    return payload,Matcher(normalize_sensor(p) for p in payload['products'])


def matching_export(reference,match):
    return [{'Артикул конкурента':reference.model,'Наша модель':match.candidate.model,'Статус':STATUS_LABELS[match.status],
             'Приоритет':match.priority,'Назначение':match.profile,'Версия правил':VERSION,
             'Версия расшифровки':reference.decoding.get('version',''),
             'Источник расшифровки':reference.decoding.get('source_url',''),**row} for row in match.rows()]
