"""Readable product markings without changing stored identities or evidence."""
import re
from urllib.parse import urlsplit


_TEKO_PREFIX = re.compile(
    r'^(?:выключатель\s+(?:индуктивный|[её]мкостный|оптический|магниточувствительный)'
    r'|(?:индуктивный|оптический)\s+датчик|датчик\s+индуктивный'
    r'|бесконтактный\s+выключатель|световая\s+завеса\s+безопасности'
    r'|датчик\s+контроля\s+минимальной\s+скорости|лазерный\s+датчик\s+расстояния'
    r'|блок\s+сопряжения|кнопка\s+сенсорная|соединитель)'
    r'(?:\s+(?:взрывозащищ[её]нный|морского\s+исполнения|для\s+автомобильного\s+транспорта))?'
    r'\s*[:—–-]?\s+', re.IGNORECASE,
)


def normalize_article(source, value):
    """Remove known TEKO product-type labels; keep the marking verbatim."""
    text = str(value or '').strip()
    if str(source or '').strip().casefold() not in ('teko', 'теко'):
        return text
    candidate = re.sub(r'^(?:ТЕКО|TEKO)\s+', '', text, flags=re.IGNORECASE)
    candidate = _TEKO_PREFIX.sub('', candidate, count=1)
    candidate = re.sub(r'^(?:ТЕКО|TEKO)\s+', '', candidate, flags=re.IGNORECASE)
    # A generic category must not disappear or become a guessed model.
    return candidate if candidate and re.search(r'\d', candidate) else text


def display_article(row):
    """Format a row for UI/export while preserving its raw article and rule_id."""
    source = row.get('source') or row.get('manufacturer') or row.get('brand')
    if not source:
        try:
            host = urlsplit(str(row.get('product_url') or row.get('url') or '')).hostname
        except ValueError:
            host = ''
        if host in ('teko-com.ru', 'www.teko-com.ru'):
            source = 'teko'
    return normalize_article(source, row.get('model') or row.get('article') or row.get('title'))
