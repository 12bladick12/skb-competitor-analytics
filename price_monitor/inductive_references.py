"""Exact reference records: an order ID is not a universal designation key.

Reviewed 2026-10-01. Add a model only with its own official source and identity;
never copy a neighboring model or an advertised replacement's specifications.
"""

REFERENCES = {
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
            return {**entry,'values':dict(entry['values']),
                    'notes':['Точная запись официальной модели. Соседние артикулы и другие исполнения эти значения не наследуют.'] +
                            (['Ток 100 мА — максимум; документ содержит температурное снижение допустимого тока (стр. 4). Требуется проверка условий.'] if order=='6058030' else [])}
    return None
