"""Consume Sensoren jobs submitted by Streamlit, from an approved HTTP network.

The UI and other source workers stay in Community Cloud. This process uses the
same database, robots policy, rate limits and adapter. It is not an HTTP proxy.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import threading
import uuid

from .adapters import ADAPTERS
from .cloud import CancellationProbe
from .models import Observation, Rule, STATUS_LABELS
from .sensoren_local import ROOT, settings_from_file
from .transport import FetchError, SourceClient

NOW = "EXTRACT(EPOCH FROM clock_timestamp())"
LIVE = f"source='sensoren' AND enabled=1 AND owner=%(owner)s AND heartbeat>{NOW}-120"


class AgentStore:
    def __init__(self, settings):
        self.settings = settings

    def batch(self, sql, params=None):
        from .db_batches import batch
        return batch(self.settings,sql,params)

    def configure(self, enabled):
        # Streamlit owns schema initialization. Reconnecting an agent must not
        # run DDL or take schema locks while other sources are collecting.
        rows = self.batch("""
            INSERT INTO price_monitor.external_sources AS e(source,enabled)
            SELECT 'sensoren',%(enabled)s
            WHERE (NOT EXISTS(SELECT 1 FROM price_monitor.runs WHERE state IN ('queued','running'))
              OR EXISTS(SELECT 1 FROM price_monitor.external_sources WHERE source='sensoren' AND enabled=%(enabled)s))
              AND (%(enabled)s=0 OR EXISTS(SELECT 1 FROM price_monitor.worker_lease
                  WHERE owner LIKE 'router%%' AND heartbeat>EXTRACT(EPOCH FROM clock_timestamp())-120)
                  OR EXISTS(SELECT 1 FROM price_monitor.external_sources WHERE source='sensoren' AND enabled=1))
            ON CONFLICT(source) DO UPDATE SET enabled=excluded.enabled,
              owner=CASE WHEN e.enabled=excluded.enabled THEN e.owner ELSE '' END,
              heartbeat=CASE WHEN e.enabled=excluded.enabled THEN e.heartbeat ELSE 0 END
            RETURNING source,enabled
            """, {"enabled": int(enabled)})
        if not rows:
            raise RuntimeError("Откройте обновлённое приложение Streamlit и завершите или остановите текущий запуск")

    def acquire(self, owner):
        return bool(self.batch(f"""
            WITH acquired AS (
              UPDATE price_monitor.external_sources SET owner=%(owner)s,heartbeat={NOW},protocol=2
              WHERE source='sensoren' AND enabled=1
                AND (owner=%(owner)s OR owner='' OR heartbeat<{NOW}-120)
              RETURNING source
            ), recovered AS (
              UPDATE price_monitor.jobs j SET state='pending'
              FROM price_monitor.rules q,acquired a
              WHERE j.rule_id=q.id AND q.source=a.source AND j.state='processing'
                AND NOT EXISTS(SELECT 1 FROM price_monitor.observations o WHERE o.job_id=j.id)
              RETURNING j.id
            ) SELECT source FROM acquired
            """, {"owner": owner}))

    def heartbeat(self, owner):
        return bool(self.batch(f"UPDATE price_monitor.external_sources SET heartbeat={NOW} "
                               f"WHERE {LIVE} RETURNING source", {"owner": owner}))

    def release(self, owner):
        self.batch("UPDATE price_monitor.external_sources SET owner='',heartbeat=0 "
                   "WHERE source='sensoren' AND owner=%(owner)s", {"owner": owner})

    def claim(self, owner):
        rows = self.batch(f"""
            WITH candidate AS (
              SELECT j.id FROM price_monitor.jobs j
              JOIN price_monitor.rules q ON q.id=j.rule_id
              JOIN price_monitor.runs r ON r.id=j.run_id
              WHERE q.source='sensoren' AND j.state='pending' AND r.state='running'
                AND r.cancel_requested=0
                AND EXISTS(SELECT 1 FROM price_monitor.external_sources WHERE {LIVE})
              ORDER BY j.id LIMIT 1
            ), claimed AS (
              UPDATE price_monitor.jobs SET state='processing'
              WHERE id IN (SELECT id FROM candidate) RETURNING id,run_id,rule_id
            ) SELECT j.id AS job_id,j.run_id,q.source,q.manufacturer,q.article,
                     q.product_url,q.url_template,COALESCE(p.reason,'') AS stop_reason
              FROM claimed j JOIN price_monitor.rules q ON q.id=j.rule_id
              LEFT JOIN price_monitor.source_pauses p ON p.run_id=j.run_id AND p.source=q.source
            """, {"owner": owner})
        return rows[0] if rows else None

    def cancelled(self, run_id, owner):
        rows = self.batch(f"""SELECT r.cancel_requested,
            EXISTS(SELECT 1 FROM price_monitor.external_sources WHERE {LIVE}) AS owns
            FROM price_monitor.runs r WHERE r.id=%(run_id)s AND r.state='running'""",
            {"owner": owner, "run_id": run_id})
        return not rows or bool(rows[0]['cancel_requested']) or not rows[0]['owns']

    def record(self, job_id, result, owner, stop_reason=""):
        from .catalog_schema import document
        fingerprint,canonical,details=document(result.details_json)
        values = {**asdict(result), "job_id": job_id, "owner": owner, "stop_reason": stop_reason}
        values.update(fingerprint=fingerprint,document=canonical,details_json=json.dumps({'ref':fingerprint}) if fingerprint else '{}',
                      category=details.get('category',''),attributes_count=len(details.get('attributes',[])))
        # Cancellation owns the outcome if it was requested before this commit.
        # The lease check fences an old agent even if it finishes a late response.
        rows = self.batch(f"""
            WITH eligible AS (
              SELECT j.id,j.run_id,j.rule_id,r.cancel_requested FROM price_monitor.jobs j
              JOIN price_monitor.runs r ON r.id=j.run_id
              JOIN price_monitor.rules q ON q.id=j.rule_id
              WHERE j.id=%(job_id)s AND j.state='processing' AND q.source='sensoren'
                AND r.state='running'
                AND EXISTS(SELECT 1 FROM price_monitor.external_sources WHERE {LIVE})
            ), saved AS (
              INSERT INTO price_monitor.observations(job_id,status,url,title,price,currency,
                availability,price_text,availability_text,detail,checked_at,http_status,response_hash,details_json)
              SELECT id,CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE %(status)s END,
                %(url)s,%(title)s,CASE WHEN cancel_requested=1 THEN NULL ELSE %(price)s END,
                CASE WHEN cancel_requested=1 THEN NULL ELSE %(currency)s END,
                %(availability)s,%(price_text)s,%(availability_text)s,
                CASE WHEN cancel_requested=1 THEN 'Остановлено пользователем' ELSE %(detail)s END,
                %(checked_at)s,%(http_status)s,%(response_hash)s,%(details_json)s FROM eligible
              ON CONFLICT(job_id) DO NOTHING RETURNING job_id
            ), document_saved AS (
              INSERT INTO price_monitor.product_documents(fingerprint,details_json)
              SELECT %(fingerprint)s,%(document)s WHERE %(fingerprint)s<>'' AND EXISTS(SELECT 1 FROM saved)
              ON CONFLICT(fingerprint) DO NOTHING RETURNING fingerprint
            ), indexed AS (
              INSERT INTO price_monitor.product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at)
              SELECT e.rule_id,%(title)s,%(category)s,lower(q.source||' '||q.manufacturer||' '||q.article||' '||%(title)s||' '||%(category)s),
                %(fingerprint)s,%(attributes_count)s,%(checked_at)s
              FROM eligible e JOIN saved s ON s.job_id=e.id JOIN price_monitor.rules q ON q.id=e.rule_id
              WHERE %(fingerprint)s<>'' AND e.cancel_requested=0
              ON CONFLICT(rule_id) DO UPDATE SET title=excluded.title,category=excluded.category,search_text=excluded.search_text,
                details_hash=excluded.details_hash,attributes_count=excluded.attributes_count,updated_at=excluded.updated_at RETURNING rule_id
            ), paused AS (
              INSERT INTO price_monitor.source_pauses(run_id,source,reason)
              SELECT run_id,'sensoren',%(stop_reason)s FROM eligible
              WHERE %(stop_reason)s<>'' AND cancel_requested=0
              ON CONFLICT(run_id,source) DO UPDATE SET reason=excluded.reason RETURNING run_id
            ) UPDATE price_monitor.jobs SET state='done'
              WHERE id IN (SELECT job_id FROM saved) RETURNING id
            """, values)
        return bool(rows)


class SensorenAgent:
    def __init__(self, store, output=None):
        self.store = store
        self.owner = str(uuid.uuid4())
        self.stopping = threading.Event()
        self.output = Path(output or ROOT/'data'/'sensoren_agent')
        self.output.mkdir(parents=True, exist_ok=True)

    def heartbeat_loop(self, finished):
        while not finished.wait(15):
            try:
                if not self.store.heartbeat(self.owner):
                    self.stopping.set()
                    return
            except Exception as exc:
                print("Связь сборщика с базой прервана: " + type(exc).__name__, flush=True)
                self.stopping.set()
                return

    def run(self):
        if not self.store.acquire(self.owner):
            raise RuntimeError("Маршрут Sensoren выключен или уже подключён другой сборщик")
        finished = threading.Event()
        heartbeat = threading.Thread(target=self.heartbeat_loop, args=(finished,), daemon=True)
        heartbeat.start()
        client, current_run, failures = None, None, 0
        print("Sensoren подключён к очереди Streamlit. Нажимайте «Запустить сбор» в приложении.", flush=True)
        try:
            while not self.stopping.is_set():
                job = self.store.claim(self.owner)
                if not job:
                    from .catalog_storage import CatalogRepository
                    from .catalog import process_catalog
                    repository=CatalogRepository(settings=self.store.settings)
                    catalogs=repository.batch("""SELECT s.run_id FROM catalog_sources s JOIN runs r ON r.id=s.run_id
                        WHERE s.source='sensoren' AND s.state IN ('pending','running') AND r.state='running' AND r.cancel_requested=0 ORDER BY s.run_id LIMIT 1""")
                    if catalogs:
                        process_catalog(repository,catalogs[0]['run_id'],'sensoren',self.owner,self.stopping)
                        continue
                    self.stopping.wait(5)
                    continue
                if current_run != job['run_id']:
                    if client:
                        client.close()
                    current_run, failures = job['run_id'], 0
                    cancelled = CancellationProbe(
                        lambda: self.store.cancelled(current_run, self.owner), self.stopping, ttl=3)
                    client = SourceClient('sensoren', cancelled=cancelled)
                rule = Rule(**{k:job[k] for k in ('source','manufacturer','article','product_url','url_template')})
                stop_reason = job['stop_reason']
                path = self.output/f"job_{job['job_id']}.json"
                restored = None
                if path.exists():
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    if payload['job']['job_id']==job['job_id'] and payload['job']['run_id']==job['run_id']:
                        restored = Observation(**payload['observation'])
                        stop_reason = payload.get('stop_reason', stop_reason)
                if restored:
                    result = restored
                elif stop_reason:
                    result = Observation('source_stopped', rule.url, detail=stop_reason)
                else:
                    try:
                        url, status, html = client.fetch(rule.url)
                        result = ADAPTERS['sensoren'].parse(rule, html, url, status)
                        failures = 0
                    except FetchError as exc:
                        result = Observation(exc.status, rule.url, detail=str(exc), http_status=exc.http_status)
                        failures = failures + 1 if exc.status in {'network_error','http_error'} else 0
                        if exc.stop_source or failures >= 3:
                            stop_reason = str(exc)
                    except Exception as exc:
                        result = Observation('parse_error', rule.url, detail='Ошибка сборщика: '+type(exc).__name__)
                if self.stopping.is_set():
                    break
                # Retain received values if the database goes away during save.
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'job':job,'observation':asdict(result),'stop_reason':stop_reason},ensure_ascii=False,indent=2),encoding='utf-8')
                temporary.replace(path)
                saved = self.store.record(job['job_id'], result, self.owner, stop_reason)
                print(f"Запуск №{current_run}: {rule.article} — "
                      + (STATUS_LABELS[result.status] if saved else 'результат не записан: отмена или потеря соединения')
                      + (f"; {result.price} {result.currency}" if saved and result.price else ''), flush=True)
        finally:
            finished.set()
            heartbeat.join(timeout=30)
            if client:
                client.close()
            try:
                self.store.release(self.owner)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description='Сборщик Sensoren для кнопки запуска в Streamlit')
    parser.add_argument('--secrets', type=Path, default=ROOT/'.streamlit'/'secrets.toml')
    parser.add_argument('--fresh-connections', action='store_true',
        help='Короткие подключения к базе, если сеть обрывает постоянные соединения')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--enable', action='store_true', help='Подключить внешний сборщик и ждать заданий')
    mode.add_argument('--disable', action='store_true', help='Вернуть обработку Sensoren облачному сборщику')
    args = parser.parse_args()
    try:
        store = AgentStore(settings_from_file(args.secrets))
        if args.fresh_connections:
            store.settings['reuse_connections'] = False
        if args.disable:
            store.configure(False)
            print('Sensoren возвращён облачному сборщику. Доступ из облака всё ещё зависит от сайта.')
            return 0
        stopping = threading.Event()
        agent = None
        def stop(*_):
            stopping.set()
            if agent:
                agent.stopping.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop)
        if args.enable:
            while not stopping.is_set():
                try:
                    store.configure(True)
                    break
                except Exception as exc:
                    message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                    print('Ожидание готовности Streamlit: '+message, flush=True)
                    stopping.wait(10)
        agent = SensorenAgent(store)
        while not stopping.is_set():
            # Recover with the same lease identity after a temporary disconnect.
            # A failed release must not make us wait for our own 120-second lease.
            agent.stopping.clear()
            try:
                agent.run()
            except Exception as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                print('Ожидание подключения: '+message, flush=True)
            if not stopping.is_set():
                stopping.wait(10)
        return 0
    except Exception as exc:
        # Only our own fixed messages are safe to show; libpq errors can include secrets.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('Сборщик не запущен или остановлен: ' + message, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
