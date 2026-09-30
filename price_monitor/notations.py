"""Versioned, manufacturer-scoped rules. Unknown series never inherit defaults."""
import re

from . import megak_notation

VERSION = 'notation-registry-2026-09-30-v1'
CHECKED = '2026-09-30'
LANBAO_PDF = 'https://www.cnlanbaosensor.com/uploads/LR18XB-Y-DC-34-E2.pdf'
SENSOR_URL = 'https://sensor-com.ru/baza-znaniy/rasshifrovka-markirovki-inductive-datchikov/'
REGISTRY = {
    'МЕГА-К': (megak_notation.SOURCE_URL, 'PS2/VB2; остальные серии требуют паспорта', 'Правила обозначений'),
    'LANBAO': (LANBAO_PDF, 'LR12XB-Y F04/N08 и LR18XB-Y F08/N12, DNO/DNC/DNR/DPO/DPC/DPR, -E2', 'Точные таблицы моделей; даты редакций не установлены'),
    'СЕНСОР': (SENSOR_URL, 'ВБИ/ВБЕ: корпус, подключение, 4 цифры электрической группы', 'Правила обозначений; публикация 23.09.2026'),
    'Autonics': ('https://www.autonics.com/us/model/PR18-5DP', 'PR18-5DP; другие исполнения требуют отдельного паспорта', 'Точная карточка модели'),
    'Balluff': ('https://assets.balluff.com/WebBinary1/MAN_BES_M08_12_18E_1_L01C_S04G_L04_X_J2_DOK_949852_AA_000.pdf', 'BES: код заказа и обозначение различаются; перенос на другие серии запрещён', 'Документ найден; автоматическое дополнение не подтверждено'),
    'BESKONTA': ('https://beskonta.ru/informaciya/rasshifrovka-markirovki/', 'Правила зависят от семейства; нужна сверка исполнения', 'Документ найден; автоматическое дополнение не подтверждено'),
    'ТЕКО': ('https://teko-com.ru/pdf/1-induktivnye.pdf', 'ISB/ISN: каталог содержит несколько поколений и специальных серий', 'Документ найден; автоматическое дополнение не подтверждено'),
    'ifm': ('https://www.ifm.com/de/de/product/IGT200', 'Короткий артикул проверяется по индивидуальной карточке', 'Карточка найдена; общей расшифровки не подтверждено'),
    'Pepperl+Fuchs': ('https://blog.pepperl-fuchs.com/en/2019/how-is-a-type-code-from-pepperlfuchs-structured/', 'Функциональный принцип, корпус, электрический выход; схема опубликована в 2019 г.', 'Документ найден; применимость к текущей серии требует проверки'),
    'SICK': ('https://www.sick.com/media/pdf/9/69/569/dataSheet_IM18-08BPS-ZC1_7900085_en.pdf', 'IM18-08BPS-ZC1, паспорт 16.02.2026; для других моделей нужен их паспорт', 'Точный паспорт найден; монтаж требует сверки таблицы и чертежа'),
}


def manufacturer(record):
    if megak_notation.is_megak(record): return 'МЕГА-К'
    raw = re.sub(r'[^a-zа-я0-9]', '', str(record.get('manufacturer') or record.get('brand') or '').casefold())
    for brand in REGISTRY:
        if raw == re.sub(r'[^a-zа-я0-9]', '', brand.casefold()): return brand
    return None


def decode(brand, model):
    url, scope, status = REGISTRY[brand]
    code = re.sub(r'\s+', '', str(model).upper()).replace('–', '-').replace('—', '-')
    result = dict(model=model, brand=brand, version=VERSION, source_url=url, checked_at=CHECKED,
                  supported=False, complete=False, family=None, fields={}, special=[], extras={},
                  notes=[], requires_review=False)
    def add(name, value, token, section='Таблица модели'):
        result['fields'][name] = dict(value=value, token=token, section=section, default=False)
    if brand == 'LANBAO':
        match = re.fullmatch(r'LR(?P<size>12|18)XB(?P<mount>F04|N08|F08|N12)D(?P<output>N|P)(?P<function>O|C|R)Y-E2', code)
        if match and match['mount'] not in ({'F04','N08'} if match['size']=='12' else {'F08','N12'}):match=None
        if match:
            quasi = match['mount'].startswith('F')
            small = match['size']=='12'
            if small:result['source_url']='https://www.cnlanbaosensor.com/uploads/LR12XB-Y-DC-3-E2.pdf'
            values = dict(body_type='threaded', diameter=float(match['size']), length=63. if quasi else 71. if small else 75.,
                          sn=float(match['mount'][1:]), mount='quasi-flush' if quasi else 'non-flush',
                          output={'N':'NPN','P':'PNP'}[match['output']],
                          function={'O':'NO','C':'NC','R':'NO/NC'}[match['function']],
                          voltage_type='DC', vmin=10., vmax=30., tmin=-25., tmax=70.,
                          ip=('67',), load=200., frequency=(800. if quasi else 500.) if small else (400. if quasi else 200.),
                          connection='connector', connector='m12')
            for name, value in values.items(): add(name, value, code)
            result['family'] = 'inductive'
            result['notes'].append('Паспорт Ver. A 04/T не содержит однозначной даты редакции. Проверьте применимость к вашему изделию; шаг резьбы, материал и контакты не выводятся по сходству.')
            if quasi:
                result['requires_review'] = True
                result['notes'].append('Quasi-flush — отдельное исполнение монтажа, не равное flush. Требуется сверка установочных размеров.')
    elif brand == 'Autonics' and code == 'PR18-5DP':
        for name, value in dict(diameter=18., sn=5., mount='flush', frequency=500., voltage_type='DC',
                                vmin=10., vmax=30., load=200., tmin=-25., tmax=70., ip=('67',), connection='cable', material='brass').items():
            add(name, value, code)
        result['family'] = 'inductive'
    elif brand == 'СЕНСОР':
        match = re.fullmatch(r'ВБ(?P<family>И|Е)-(?P<body>МА|Д|М|П|Ф|Ц|Щ)(?P<size>\d+)-(?P<length>\d+)(?P<conn>УР|ВР|Р8|У|В|С|К|Р)-(?P<mount>[12])(?P<supply>[123])(?P<output>[1234578])(?P<function>[1234])(?:-(?P<mods>[А-ЯA-Z0-9.]+))?', code)
        if match:
            result['family'] = 'inductive' if match['family']=='И' else 'capacitive'
            body = match['body']
            if body in ('М','МА','Ц','Д'):
                add('body_type', 'smooth' if body=='Д' else 'threaded', body)
                add('diameter', float(match['size']), match['size'])
            elif body in ('П','Щ'): add('body_type', 'rectangular' if body=='П' else 'slot', body)
            # Manufacturer calls this a conditional length; not a measured body length.
            result['extras']['Условная длина/высота'] = match['length']
            if body in ('Д','М','МА','П','Ц','Щ'): add('material', 'stainless' if body=='МА' else 'brass' if body in ('Д','М') else 'plastic', body)
            conn = match['conn']
            add('connection', 'cable+connector' if conn in ('УР','ВР') else 'connector' if conn in ('Р','Р8') else 'terminals' if conn=='К' else 'cable', conn)
            if conn in ('УР','ВР','Р','Р8'): add('connector', 'm8' if conn=='Р8' else 'm12', conn)
            add('mount', 'flush' if match['mount']=='1' else 'non-flush', match['mount'])
            voltage = {'1':('DC',10.,30.),'2':('AC',100. if match['family']=='Е' else 20.,250.),'3':('AC/DC',20.,250.)}[match['supply']]
            for name, value in zip(('voltage_type','vmin','vmax'), voltage): add(name, value, match['supply'])
            add('output', {'1':'PNP','2':'NPN','3':'2-wire','4':'2-wire','5':'2-wire','7':'relay','8':'NPN/PNP'}[match['output']], match['output'])
            add('function', {'1':'NO','2':'NC','3':'NO/NC','4':'configurable'}[match['function']], match['function'])
            if body=='Щ': result['special'].append('slot')
            if match['mods'] or match['function']=='4':
                result['requires_review']=True
                result['notes'].append('Дополнительные модификации/программируемый выход требуют отдельной проверки; температура, IP и Sn не подставлены.')
    if result['fields']:
        result['supported'] = result['complete'] = True
    else:
        result['notes'].append('Автоматическое дополнение для этого точного обозначения не подтверждено. '+scope+'. '+status+'.')
    return result


def enrich(sensor, record):
    brand = manufacturer(record)
    if not brand: return sensor
    if brand == 'МЕГА-К':
        sensor=megak_notation.enrich(sensor, record)
        sensor.decoding.update(brand=brand, checked_at=CHECKED)
        return sensor
    decoded = decode(brand, sensor.model)
    sensor.decoding = decoded
    if not decoded['supported']: return sensor
    if sensor.family and sensor.family != decoded['family']:
        decoded['requires_review']=True
        decoded['notes'].append('Тип изделия противоречит паспорту; дополнение остановлено.')
        return sensor
    sensor.family = decoded['family']
    sensor.special=tuple(sorted(set(sensor.special)|set(decoded['special'])))
    confirmed=set()
    for name, entry in decoded['fields'].items():
        existing=sensor.values.get(name)
        entry['card_value']=existing
        if name in sensor.conflicts: entry['application']='card_conflict'; decoded['requires_review']=True
        elif existing is None:
            sensor.values[name]=entry['value'];entry['application']='filled'
            sensor.raw.setdefault(name,[]).append(dict(field='Паспорт '+brand, value=sensor.model, source_url=decoded['source_url'], version=VERSION))
        elif existing == entry['value']:
            entry['application']='confirmed'
            confirmed.add({'vmin':'voltage','vmax':'voltage','voltage_type':'voltage',
                           'tmin':'temperature','tmax':'temperature','body_type':'body','diameter':'body','pitch':'body'}.get(name,name))
        else: entry['application']='conflict';decoded['requires_review']=True
    if len(confirmed) < 2:
        decoded['requires_review']=True
        decoded['notes'].append('Недостаточно независимых совпадений с карточкой (нужно минимум два). Дополненные значения требуют проверки актуального паспорта.')
    return sensor


def field_source(sensor, name):
    text=megak_notation.field_source(sensor,name)
    return text.replace('МЕГА-К',sensor.decoding.get('brand','МЕГА-К'))
