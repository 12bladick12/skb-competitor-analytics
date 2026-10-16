"""Brand membership is explicit; a name match never establishes participation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def entities():
    return deepcopy(json.loads(Path(__file__).with_name('entities.json').read_text(encoding='utf-8')))


def fingerprint(rows):
    # Refresh timestamps and evidence transport do not reset a 14-day trial.
    fields=('id','brand','inn','ogrn','name','names','address','active_from','active_to','membership_sources','history_verified')
    stable=[{k:row.get(k) for k in fields} for row in sorted(rows,key=lambda r:r['id'])]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_entity(row):
    inn, ogrn = row.get('inn', ''), row.get('ogrn', '')
    if not (len(inn) == 10 and inn.isdigit() and len(ogrn) == 13 and ogrn.isdigit()):
        raise ValueError('Для юридического лица нужны ИНН из 10 цифр и ОГРН из 13 цифр.')
    if sum(int(n)*w for n,w in zip(inn[:9], (2,4,10,3,5,9,4,6,8))) % 11 % 10 != int(inn[-1]):
        raise ValueError('Контрольная цифра ИНН не совпадает.')
    if int(ogrn[:-1]) % 11 % 10 != int(ogrn[-1]):
        raise ValueError('Контрольная цифра ОГРН не совпадает.')
    if not row.get('membership_sources') or not row.get('names') or not row.get('brand'):
        raise ValueError('Не подтверждена связь юридического лица с брендом.')
    return row
