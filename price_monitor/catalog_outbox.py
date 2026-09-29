"""Durable local handoff: a fetched page survives a failed database commit."""
from dataclasses import asdict
import json
from pathlib import Path
import re

from .models import Rule, Observation


class CatalogOutbox:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)

    def path(self, page):
        if not re.fullmatch(r'[a-f0-9]{64}',page['id']):raise ValueError('Invalid catalog page identifier')
        return self.root/(page['id']+'.json')

    def save(self, page, results):
        target=self.path(page)
        payload={'page':{k:page[k] for k in ('id','run_id','source','url')},
                 'results':[{'rule':asdict(rule),'observation':asdict(obs)} for rule,obs in results]}
        temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
        temporary.replace(target)

    def load(self, page):
        target=self.path(page)
        if not target.exists():return None
        saved=json.loads(target.read_text(encoding='utf-8'))
        if saved['page']!={k:page[k] for k in ('id','run_id','source','url')}:
            raise ValueError('Catalog outbox identity mismatch')
        return [(Rule(**row['rule']),Observation(**row['observation'])) for row in saved['results']]

    def acknowledge(self, page):
        self.path(page).unlink(missing_ok=True)
