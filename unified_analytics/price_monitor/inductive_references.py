"""Exact reference records: an order ID is not a universal designation key.

Reviewed 2026-10-01. Add a model only with its own official source and identity;
never copy a neighboring model or an advertised replacement's specifications.
"""

REFERENCES = {
    ('ТЕКО','IV0BAC41B-49P-6-LZS4'):dict(
        aliases=('ДАТЧИККОНТРОЛЯМИНИМАЛЬНОЙСКОРОСТИIV0BAC41B-49P-6-LZS4',),
        source='https://teko-com.ru/local/ajax/file_download.php?id=520488',
        section='IV0B AC41B-49P-6-LZS4.000 ПС, PDF стр. 2: Формат М18×1×84 / габаритный чертёж; дата не указана',
        review=True,special=['speed'],
        values=dict(body_type='threaded',diameter=18.,pitch=1.,length=84.)),
    ('Balluff','BES001P'):dict(
        aliases=('BESM08MG-USC20B-BP03',),
        source='https://www.balluff.com/pt-pt/products/BES001P',
        section='BES001P / BES M08MG-USC20B-BP03, Dimension / Style Housing; дата не указана',
        values=dict(body_type='threaded',diameter=8.,pitch=1.,length=40.)),
    ('Balluff','BES001Y'):dict(
        aliases=('BESM08ME1-USC20B-S04G',),
        source='https://www.balluff.com/en-lt/products/BES001Y',
        section='BES001Y / BES M08ME1-USC20B-S04G, Dimension / Style Housing; дата не указана',
        values=dict(body_type='threaded',diameter=8.,pitch=1.,length=50.)),
    ('ifm','IFC204'):dict(
        aliases=('IFB3004BBPKG/US-104',),
        source='https://www.ifm.com/in/en/product/IFC204',
        section='IFC204-02 EN-GB, 2025-01-14, Mechanical data: Thread designation / Dimensions',
        values=dict(body_type='threaded',diameter=12.,pitch=1.,length=45.)),
    ('SICK','6058028'):dict(
        aliases=('IM04-01BNSVU2K',),
        source='https://www.sick.com/media/pdf/4/44/044/dataSheet_IM04-01BNSVU2K_6058028_en.pdf',
        section='6058028 / IM04-01BNSVU2K, 2026-06-10, стр. 2: Thread size',
        values=dict(body_type='threaded',diameter=4.,pitch=.5)),
    ('SICK','6058029'):dict(
        aliases=('IM04-01BPSVU2K',),
        source='https://www.sick.com/media/pdf/5/45/045/dataSheet_IM04-01BPSVU2K_6058029_en.pdf',
        section='6058029 / IM04-01BPSVU2K, 2026-08-21, стр. 2–4: Thread size / Housing length',
        values=dict(body_type='threaded',diameter=4.,pitch=.5,length=12.)),
    ('SICK','6058031'):dict(
        aliases=('IM04-01BPSVR8K',),
        source='https://www.sick.com/media/pdf/7/47/047/dataSheet_IM04-01BPSVR8K_6058031_en.pdf',
        section='6058031 / IM04-01BPSVR8K, стр. 2: Thread size (дата редакции не подтверждена)',
        values=dict(body_type='threaded',diameter=4.,pitch=.5)),
    ('Balluff','BES005N'):dict(
        aliases=('BESM12MI-POC40B-S04G',),
        source='https://www.balluff.com/en-us/products/BES005N',
        section='BES005N / BES M12MI-POC40B-S04G: Key features',
        values=dict(body_type='threaded',diameter=12.,pitch=1.,length=65.,mount='flush',sn=4.,
                    output='PNP',function='NC',frequency=2500.,material='brass',connection='connector',
                    connector='m12',pin_count=4.,voltage_type='DC',vmin=10.,vmax=30.,tmin=-25.,tmax=70.,ip=('68',))),
    ('SICK','1040764'):dict(
        aliases=('IME12-04BPSZC0S',),
        source='https://www.sick.com/media/pdf/1/81/481/dataSheet_IME12-04BPSZC0S_1040764_en.pdf',
        section='1040764 / IME12-04BPSZC0S, pp. 2–3, 2026-07-18',
        values=dict(body_type='threaded',diameter=12.,pitch=1.,length=65.,mount='flush',sn=4.,
                    output='PNP',function='NO',frequency=2000.,material='brass',connection='connector',
                    connector='m12',pin_count=4.,wire_count=3.,voltage_type='DC',vmin=10.,vmax=30.,
                    tmin=-25.,tmax=75.,ip=('67',),load=200.)),
    ('ifm','IGT200'):dict(
        aliases=('IGB3012-BPKG/V4A/US-104',),
        source='https://www.ifm.com/restservices/gb/en/assets/c3VwcGxpZXJzL2lmbS9kb2N1bWVudHMvcHJvZHVjdC9JR1QyMDAtMDAvZGF0ZW5ibGFldHRlci9JR1QyMDAtMDBfRU4tR0IucGRm',
        section='IGT200-00 EN-GB, 2023-04-13, Product characteristics / Outputs',
        values=dict(body_type='threaded',diameter=18.,pitch=1.,length=51.,sn=12.,output='PNP',function='NO',
                    voltage_type='DC',vmin=10.,vmax=36.,load=100.,frequency=300.,
                    mount='non-flush',material='stainless',tmin=0.,tmax=100.,ip=('68','69K'),
                    connection='connector',connector='m12',pin_count=4.)),
    ('SICK','6058030'):dict(
        aliases=('IM04-01BNSVR8K',),
        source='https://www.sick.com/media/pdf/6/46/046/dataSheet_IM04-01BNSVR8K_6058030_en.pdf',
        section='6058030 / IM04-01BNSVR8K, pp. 2–4, 2026-06-10',review=True,
        values=dict(body_type='threaded',diameter=4.,pitch=.5,length=12.,sn=1.,mount='flush',output='NPN',function='NO',
                    frequency=8000.,voltage_type='DC',vmin=10.,vmax=30.,load=100.,material='stainless',
                    tmin=-25.,tmax=70.,ip=('67',),connection='cable+connector',connector='m8',pin_count=3.,wire_count=3.)),
}


def exact_reference(brand, code):
    for (owner,order),entry in REFERENCES.items():
        if owner==brand and code in (order,*entry['aliases']):
            return {**entry,'values':dict(entry['values']),'geometry_fields':('body_type','diameter','pitch','length'),
                    'notes':['Точная запись официальной модели. Соседние артикулы и другие исполнения эти значения не наследуют.'] +
                            (['Ток 100 мА — максимум; документ содержит температурное снижение допустимого тока (стр. 4). Требуется проверка условий.'] if order=='6058030' else [])}
    return None
