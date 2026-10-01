"""Catalog-backed identity search, paste parsing and strict characteristic filters.

Search never changes product identity or treats an absent characteristic as a match.
The comparison engine remains responsible for evaluating alternatives.
"""
from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata

from .matching import display
from .sources import SOURCES


FAMILIES = {
    'inductive': 'Индуктивный', 'capacitive': 'Ёмкостный', 'optical': 'Оптический',
    'reed': 'Герконовый / магниточувствительный', 'ultrasonic': 'Ультразвуковой',
    'level': 'Датчик уровня', 'pressure': 'Датчик давления', 'temperature': 'Датчик температуры',
}
FILTERS = {
    'family': 'Тип датчика', 'body_type': 'Форма корпуса', 'diameter': 'Диаметр корпуса, мм',
    'length': 'Длина корпуса, мм', 'dimensions': 'Габариты из карточки, мм',
    'output': 'Выходной сигнал', 'function': 'Функция выхода', 'sn': 'Расстояние срабатывания, мм',
    'mount': 'Монтаж', 'connection': 'Подключение', 'connector': 'Разъём',
    'voltage_type': 'Тип питания', 'material': 'Материал корпуса', 'ip': 'Защита IP',
}
DIMENSION_FIELDS = {
    'габаритный размер, мм', 'габаритные размеры, мм', 'размеры корпуса, мм',
    'габариты корпуса, мм', 'размер корпуса', 'dimensions', 'dimensions [mm]',
}


def identity_key(value):
    text = unicodedata.normalize('NFKC', str(value or '')).casefold().replace('ё', 'е')
    return re.sub(r'\s+', '', text.translate(str.maketrans('–—−', '---')))


def entry_label(row):
    label = str(row['brand']) + ' · ' + str(row['model'])
    article = row.get('article')
    if article and identity_key(article) != identity_key(row['model']):
        label += ' · арт. ' + str(article)
    if row.get('source'):
        source = SOURCES.get(row['source'])
        label += ' · ' + (source.label if source else str(row['source']))
    return label


def characteristic_values(row):
    sensor = row['sensor']
    values = {name: value for name, value in sensor.values.items() if name not in sensor.conflicts}
    if sensor.family:
        values['family'] = sensor.family
    attributes = row.get('props') or (row.get('_specifications') or {}).get('attributes') or []
    if isinstance(attributes, dict):
        attributes = [{'name': name, 'value': value} for name, value in attributes.items()]
    dimensions = set()
    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        if str(attr.get('name', '')).strip().casefold() in DIMENSION_FIELDS:
            raw = str(attr.get('value') or '').strip()
            # Preserve the source's order: no assumptions about L/W/H orientation.
            if re.search(r'\d\s*[xх×*]\s*\d', raw, re.I):
                dimensions.add(re.sub(r'\s+', '', raw.casefold()).replace('х', '×').replace('x', '×').replace('*', '×'))
    if len(dimensions) == 1:
        values['dimensions'] = dimensions.pop()
    return values


def characteristic_label(field, value):
    if field == 'family':
        return FAMILIES.get(value, str(value))
    return display(value, field)


@dataclass(frozen=True)
class Resolution:
    query: str
    status: str
    candidates: tuple[str, ...]


class CatalogSearch:
    def __init__(self, records):
        self.records = records
        self.aliases = defaultdict(list)
        self.text = {}
        self.values = {}
        self.max_words = 1
        for entry, row in records.items():
            aliases = [row['model'], row.get('article', ''),
                       (row.get('own_price') or {}).get('source_model', '')]
            aliases += [str(row['brand']) + ' ' + str(alias) for alias in aliases if alias]
            for alias in aliases:
                normalized = identity_key(alias)
                if normalized and entry not in self.aliases[normalized]:
                    self.aliases[normalized].append(entry)
                    self.max_words = max(self.max_words, len(re.findall(r'[^\s,]+', str(alias))))
            self.text[entry] = identity_key(entry_label(row))
            self.values[entry] = characteristic_values(row)

    def search(self, query):
        exact = self.aliases.get(identity_key(query), [])
        if exact:
            return list(exact)
        words = [identity_key(word) for word in query.split() if identity_key(word)]
        if not words:
            return []
        rows = [entry for entry, text in self.text.items() if all(word in text for word in words)]
        return sorted(rows, key=lambda entry: (
            not identity_key(self.records[entry]['model']).startswith(identity_key(query)),
            entry_label(self.records[entry]), entry))

    def split_paste(self, text):
        """Longest known name wins, including names containing spaces/commas.

        Newlines, tabs, semicolons and pipes are explicit boundaries. Within a
        line, known names/articles separate themselves; unknown phrases survive.
        """
        output = []
        for line in re.split(r'[\r\n\t;|]+', text):
            line = line.strip().strip('"')
            if not line:
                continue
            if identity_key(line) in self.aliases:
                output.append(line)
                continue
            tokens = list(re.finditer(r'[^\s,]+', line))
            cursor = 0
            unknown = []

            def flush():
                if unknown:
                    output.append(' '.join(unknown))
                    unknown.clear()

            while cursor < len(tokens):
                stop = None
                for end in range(min(len(tokens), cursor + self.max_words), cursor, -1):
                    candidate = line[tokens[cursor].start():tokens[end - 1].end()]
                    if identity_key(candidate) in self.aliases:
                        stop = end
                        break
                if stop is not None:
                    flush()
                    output.append(line[tokens[cursor].start():tokens[stop - 1].end()])
                    cursor = stop
                else:
                    if cursor and ',' in line[tokens[cursor - 1].end():tokens[cursor].start()]:
                        flush()
                    unknown.append(tokens[cursor].group())
                    cursor += 1
            flush()
        return output

    def resolve_paste(self, text):
        result = []
        seen = set()
        for query in self.split_paste(text):
            key = identity_key(query)
            if key in seen:
                continue
            seen.add(key)
            candidates = self.search(query)
            exact = self.aliases.get(key, [])
            status = 'exact' if len(exact) == 1 else 'ambiguous' if exact else 'partial' if candidates else 'missing'
            result.append(Resolution(query, status, tuple(candidates)))
        return result

    def filter(self, criteria):
        def matches(values):
            for field, expected in criteria.items():
                if expected is None:
                    continue
                actual = values.get(field)
                if actual is None or actual != expected:
                    return False
            return True
        return [entry for entry, values in self.values.items() if matches(values)]

    def options(self, field, criteria=None):
        other = {key: value for key, value in (criteria or {}).items() if key != field}
        values = {self.values[entry][field] for entry in self.filter(other) if self.values[entry].get(field) is not None}
        return sorted(values, key=lambda value: (0, value) if isinstance(value, (int, float)) else (1, characteristic_label(field, value)))


def merge_selection(selected, added, records):
    return list(dict.fromkeys(entry for entry in [*selected, *added] if entry in records))
