"""Neon's documented SQL-over-HTTPS protocol; no credentials in errors or logs.

Protocol: https://github.com/neondatabase/neon/blob/main/proxy/README.md
"""

import json
import re

from .drive_store import StorageError
from .readiness import neon_parameters, section


class NeonError(StorageError):
    def __init__(self, code=None):
        super().__init__("Общее хранилище временно недоступно. Повторите действие.")
        self.sqlstate = code if isinstance(code,str) and re.fullmatch(r'[A-Z0-9]{5}',code) else None


class NeonHTTP:
    def __init__(self, config, session=None):
        self.uri = section(config,'cloud').get('database_url')
        params = neon_parameters(self.uri)
        endpoint, suffix = params['host'].split('.',1)
        self.url = 'https://' + endpoint.removesuffix('-pooler') + '.' + suffix + '/sql'
        self.session = session

    def query(self, query, params=()):
        import requests
        session = self.session or requests.Session()
        try:
            body=json.dumps({'query':query,'params':list(params)},ensure_ascii=False).encode('utf-8')
            with session.post(self.url, data=body, headers={
                'Neon-Connection-String':self.uri,'Content-Type':'application/json'},
                timeout=(10,45), allow_redirects=False, stream=True) as response:
                data=bytearray()
                for part in response.iter_content(64*1024):
                    data.extend(part)
                    if len(data)>32*1024*1024:
                        raise NeonError()
                result=json.loads(data)
                if response.status_code!=200:
                    raise NeonError(result.get('code') if isinstance(result,dict) else None)
                if not isinstance(result,dict) or not isinstance(result.get('rows'),list):
                    raise NeonError()
                return result['rows']
        except NeonError:
            raise
        except Exception:
            raise NeonError() from None
        finally:
            if self.session is None:
                session.close()


class MigrationConnection:
    """Adapt only self-contained atomic DO commands, never pretend to hold a session."""
    connection = None

    def __init__(self, client):
        import psycopg
        self.client, self.adapters = client, psycopg.adapters

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement):
        query = statement if isinstance(statement,str) else statement.as_string(self)
        if not query.lstrip().startswith('DO '):
            raise ValueError('HTTPS migration accepts one atomic DO command only')
        return self.client.query(query)
