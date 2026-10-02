"""Local evidence storage and bounded model calls; no production DB writes."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'data' / 'agent_pilots'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def schema_object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


class ModelError(RuntimeError):
    pass


class ResponsesClient:
    """Fixed official endpoint, no tools, no retries, no credential logging.

    max_calls includes failed attempts. Cache keys include all inputs, the prompt,
    schema and requested model. A cache hit is not an independent evaluation.
    """
    def __init__(self, model, directory, max_calls=1, session=None):
        if not model or not 1 <= max_calls <= 20:
            raise ValueError('Укажите модель и лимит от 1 до 20 вызовов')
        self.model, self.directory = model, Path(directory)
        self.max_calls, self.calls = max_calls, 0
        self.session = session or requests.Session()

    def structured(self, instructions, content, schema, name):
        body = {'model': self.model, 'store': False, 'instructions': instructions,
                'input': [{'role': 'user', 'content': content}],
                'max_output_tokens': 6000,
                'text': {'format': {'type': 'json_schema', 'name': name,
                                    'strict': True, 'schema': schema}}}
        key = fingerprint(body)
        cached = self.directory / 'cache' / (key + '.json')
        if cached.exists():
            return {**read_json(cached), 'cache_hit': True}
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not api_key:
            raise ModelError('OPENAI_API_KEY не настроен; запрос не отправлен')
        if self.calls >= self.max_calls:
            raise ModelError('Достигнут лимит вызовов пилота')
        self.calls += 1
        started = time.monotonic()
        try:
            response = self.session.post('https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer ' + api_key}, json=body,
                timeout=(10, 120), allow_redirects=False)
        except requests.RequestException as exc:
            raise ModelError('API недоступен: ' + type(exc).__name__) from None
        if response.status_code != 200:
            # Provider error messages can echo input or credentials.
            raise ModelError(f'OpenAI API: HTTP {response.status_code}; повторов не было')
        try:
            raw = response.json()
            if not isinstance(raw, dict):
                raise ModelError('API вернул неверную структуру ответа')
            if raw.get('status') != 'completed':
                raise ModelError('API вернул незавершённый ответ')
            parts = [part for item in raw.get('output', []) if item.get('type') == 'message'
                     for part in item.get('content', [])]
            if any(part.get('type') == 'refusal' for part in parts):
                raise ModelError('Модель отказалась обрабатывать материал')
            text = ''.join(p['text'] for p in parts if p.get('type') == 'output_text')
            result = {'result': json.loads(text), 'model': raw.get('model', self.model),
                      'requested_model': self.model, 'usage': raw.get('usage', {}),
                      'seconds': round(time.monotonic() - started, 3),
                      'request_hash': key, 'cache_hit': False, 'created_at': time.time()}
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ModelError('Ответ API не содержит допустимый JSON') from None
        write_json(cached, result)
        return result
