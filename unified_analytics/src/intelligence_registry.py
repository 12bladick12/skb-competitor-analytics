"""Brand identity is separate from legal identity. Sites checked 2026-10-01."""
ENTITIES = {
    'teko': dict(name='ТЕКО', legal_name='АО НПК «ТЕКО»', inn='7453019186', ogrn='1027403885717',
        domains=['teko-com.ru', 'teko.ru'], aliases=['ТЕКО', 'НПК ТЕКО'],
        distinctive=['НПК ТЕКО', 'НПК «ТЕКО»'], city='Челябинск',
        registry_url='https://teko-com.ru/about/requisite.html'),
    'beskonta': dict(name='BESKONTA', legal_name='ООО «СОЧЕР»', inn='7448197440', ogrn='1167456127156',
        domains=['beskonta.ru'], aliases=['BESKONTA', 'BESCONTA', 'БЕСКОНТА', 'СОЧЕР'],
        distinctive=['BESKONTA', 'BESCONTA', 'БЕСКОНТА', 'СОЧЕР'], city='Челябинск',
        registry_url='https://beskonta.ru/o-kompanii/rekvizity/'),
    'sensor': dict(name='СЕНСОР', legal_name='ЗАО «СЕНСОР»', inn='6606013344', ogrn='1026600730749',
        domains=['sensor-com.ru'], aliases=['СЕНСОР', 'ЗАО СЕНСОР'],
        distinctive=['ЗАО «СЕНСОР»', 'ЗАО СЕНСОР'], city='Екатеринбург',
        registry_url='https://sensor-com.ru/contacts/'),
    'mega_k': dict(name='МЕГА-К', legal_name='ООО НПФ «Мега-К»', inn='4028062967', ogrn='1164027062319',
        domains=['mega-k.ru', 'mega-k.com'], aliases=['МЕГА-К', 'МЕГА К', 'MEGA-K'],
        distinctive=['МЕГА-К', 'МЕГА К', 'MEGA-K'], city='Калуга',
        registry_url='https://mega-k.com/news/requisites'),
}

for _entity in ENTITIES.values():
    _entity.update(identity_checked_at='2026-10-01', identity_basis='official_company_website',
                   egrul_verified=False)

EVENT_KINDS = {'mentions': 'Внешние упоминания', 'litigation': 'Судебные события'}
RECORD_KINDS = ('intel_mention', 'intel_case', 'intel_check')
