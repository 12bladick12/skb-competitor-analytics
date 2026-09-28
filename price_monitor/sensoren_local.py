"""Manual Sensoren collection from a network where public HTTP access works.

Uses the same robots-aware client and parser as the cloud worker. Never acts as
a proxy, bypasses a challenge, or changes the identity of the HTTP client.
Completed snapshots are appended to Supabase with an idempotent snapshot ID.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import tomllib
import uuid

from .adapters import ADAPTERS, parse_money
from .exchange import read_table, validate_rows, csv_bytes, xlsx_bytes
from .models import Observation, STATUS_LABELS, AVAILABILITY_LABELS, SUCCESS_STATUSES, utcnow
from .sources import validate_url
from .transport import SourceClient, FetchError

ROOT = Path(__file__).resolve().parents[1]

# ClientCursor binds and quotes values before sending this single SQL batch.
# The advisory lock is a separate statement: the subsequent statement receives
# a fresh READ COMMITTED snapshot, including a concurrent import's commit.
# This uses the existing schema; the cloud worker never claims completed runs.
IMPORT_SQL = """
BEGIN;
SET LOCAL statement_timeout = '20s';
SET LOCAL lock_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '30s';
SELECT pg_advisory_xact_lock(6743928101);
WITH input AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
  ordinal integer, rule_key text, source text, manufacturer text, article text,
  product_url text, url_template text, status text, url text, title text,
  price text, currency text, availability text, price_text text,
  availability_text text, detail text, checked_at text, http_status integer,
  response_hash text)
), existing AS (
 SELECT id FROM price_monitor.runs WHERE note=%(note)s
), new_run AS (
 INSERT INTO price_monitor.runs(state,created_at,started_at,finished_at,note)
 SELECT %(state)s,%(started)s,%(started)s,%(finished)s,%(note)s
 WHERE NOT EXISTS (SELECT 1 FROM existing)
 RETURNING id
), saved_rules AS (
 INSERT INTO price_monitor.rules(rule_key,source,manufacturer,article,product_url,url_template,created_at)
 SELECT i.rule_key,i.source,i.manufacturer,i.article,i.product_url,i.url_template,%(started)s
 FROM input i CROSS JOIN new_run
 ON CONFLICT(rule_key) DO UPDATE SET rule_key=excluded.rule_key
 RETURNING id,rule_key
), saved_jobs AS (
 INSERT INTO price_monitor.jobs(run_id,rule_id,state)
 SELECT n.id,r.id,'done' FROM new_run n CROSS JOIN saved_rules r
 JOIN input i ON i.rule_key=r.rule_key ORDER BY i.ordinal
 RETURNING id,rule_id
), saved_observations AS (
 INSERT INTO price_monitor.observations(job_id,status,url,title,price,currency,
  availability,price_text,availability_text,detail,checked_at,http_status,response_hash)
 SELECT j.id,i.status,i.url,i.title,i.price,i.currency,i.availability,i.price_text,
  i.availability_text,i.detail,i.checked_at,i.http_status,i.response_hash
 FROM saved_jobs j JOIN saved_rules r ON r.id=j.rule_id
 JOIN input i ON i.rule_key=r.rule_key
 RETURNING id
)
SELECT id AS imported_run_id FROM new_run UNION ALL SELECT id FROM existing;
COMMIT;
"""


def settings_from_file(path):
    with Path(path).open("rb") as file:
        settings = tomllib.load(file)["database"]
    allowed = {"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}
    settings = {key: value for key, value in settings.items() if key in allowed}
    if not {"host", "dbname", "user", "password"}.issubset(settings):
        raise ValueError("В Secrets не заполнены параметры database")
    settings.setdefault("sslmode", "require")
    if settings["sslmode"] not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("Для Supabase требуется SSL")
    return settings


def connect(settings):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(**settings, connect_timeout=10, autocommit=True,
                           cursor_factory=psycopg.ClientCursor, row_factory=dict_row)


def sensoren_rules(rows):
    rules, errors = validate_rows(rows)
    if errors:
        raise ValueError("Ошибки таблицы: " + json.dumps(errors, ensure_ascii=False))
    rules = [rule for rule in rules if rule.source == "sensoren"]
    if not rules:
        raise ValueError("В таблице нет заданий Sensoren")
    return rules


def saved_rules(settings):
    with connect(settings) as connection:
        rows = connection.execute("SELECT source,manufacturer,article,product_url,url_template "
                                  "FROM price_monitor.rules WHERE source='sensoren' ORDER BY id").fetchall()
    return sensoren_rules(rows)


def collect(rules, progress=print, client_factory=SourceClient):
    snapshot = {"version": 1, "id": str(uuid.uuid4()), "started_at": utcnow(), "rows": []}
    reason, failures, interrupted = "", 0, False
    client = client_factory("sensoren")
    try:
        for index, rule in enumerate(rules, 1):
            if reason:
                result = Observation("cancelled" if interrupted else "source_stopped", rule.url, detail=reason)
            else:
                try:
                    url, status, html = client.fetch(rule.url)
                    result = ADAPTERS["sensoren"].parse(rule, html, url, status)
                    failures = 0
                except FetchError as exc:
                    result = Observation(exc.status, rule.url, detail=str(exc), http_status=exc.http_status)
                    failures = failures + 1 if exc.status in {"network_error", "http_error"} else 0
                    if exc.stop_source or failures >= 3:
                        reason = str(exc)
                except KeyboardInterrupt:
                    reason, interrupted = "Локальный сбор остановлен пользователем", True
                    result = Observation("cancelled", rule.url, detail=reason)
                except Exception as exc:
                    result = Observation("parse_error", rule.url, detail="Ошибка локального адаптера: " + type(exc).__name__)
            snapshot["rows"].append({**asdict(rule), **asdict(result)})
            progress(f"{index}/{len(rules)} {rule.manufacturer} {rule.article}: "
                     f"{STATUS_LABELS[result.status]}" + (f"; {result.price} {result.currency}" if result.price else ""))
    finally:
        client.close()
    snapshot["finished_at"] = utcnow()
    return snapshot


def validate_snapshot(snapshot):
    if snapshot.get("version") != 1:
        raise ValueError("Неподдерживаемая версия снимка")
    uuid.UUID(snapshot["id"])
    for field in ("started_at", "finished_at"):
        if datetime.fromisoformat(snapshot[field]).tzinfo is None:
            raise ValueError("Время снимка должно включать часовой пояс")
    rows = snapshot["rows"]
    rules, errors = validate_rows(rows)
    if errors or not rules or len(rows) != len(rules) or any(r.source != "sensoren" for r in rules):
        raise ValueError("Снимок должен содержать корректные уникальные задания Sensoren")
    values = []
    for index, (rule, row) in enumerate(zip(rules, rows)):
        result = Observation(**{key: row[key] for key in Observation.__dataclass_fields__})
        if result.status not in STATUS_LABELS or result.status in {"pending", "processing"}:
            raise ValueError("Снимок содержит незавершённое задание")
        validate_url("sensoren", result.url, product=False)
        if result.availability not in AVAILABILITY_LABELS or datetime.fromisoformat(result.checked_at).tzinfo is None:
            raise ValueError("Некорректное наличие или время результата")
        if result.status == "priced":
            if not isinstance(result.price, str) or parse_money(result.price) != result.price or not result.currency:
                raise ValueError("Некорректная цена в снимке")
        elif result.price is not None:
            raise ValueError("Цена указана у результата без полученной цены")
        values.append({**asdict(rule), **asdict(result), "rule_key": rule.key, "ordinal": index})
    return values


def save_snapshot(settings, snapshot):
    values = validate_snapshot(snapshot)
    state = "completed" if all(row["status"] in SUCCESS_STATUSES for row in values) else "completed_with_errors"
    if any(row["status"] == "cancelled" for row in values):
        state = "cancelled"
    params = {"rows": json.dumps(values, ensure_ascii=False), "state": state,
              "started": snapshot["started_at"], "finished": snapshot["finished_at"],
              "note": "Sensoren: локальный сбор HTTP; снимок " + snapshot["id"]}
    run_id = None
    with connect(settings) as connection:
        cursor = connection.execute(IMPORT_SQL, params)
        while True:
            if cursor.description and cursor.description[0].name == "imported_run_id":
                run_id = cursor.fetchone()["imported_run_id"]
            if not cursor.nextset():
                break
    if run_id is None:
        raise RuntimeError("База не вернула номер запуска; повторите сохранение того же снимка")
    return run_id


def export_snapshot(snapshot, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / snapshot["id"]
    stem.with_suffix(".json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    stem.with_suffix(".csv").write_bytes(csv_bytes(snapshot["rows"]))
    stem.with_suffix(".xlsx").write_bytes(xlsx_bytes(snapshot["rows"]))
    return stem.with_suffix(".json")


def main():
    parser = argparse.ArgumentParser(description="Сбор Sensoren с компьютера и сохранение в Supabase")
    parser.add_argument("--secrets", type=Path, default=ROOT / ".streamlit" / "secrets.toml")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--file", type=Path, help="CSV/XLSX заданий; по умолчанию — сохранённые правила Sensoren из Supabase")
    group.add_argument("--resume", type=Path, help="Повторить сохранение готового JSON-снимка без нового сбора")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "sensoren_runs")
    args = parser.parse_args()
    try:
        settings = settings_from_file(args.secrets)
        if args.resume:
            snapshot = json.loads(args.resume.read_text(encoding="utf-8"))
            snapshot_file = args.resume
        else:
            rules = sensoren_rules(read_table(args.file.read_bytes(), args.file.name)) if args.file else saved_rules(settings)
            snapshot = collect(rules)
            snapshot_file = export_snapshot(snapshot, args.output)
            print(f"Локальные XLSX/CSV и снимок сохранены: {snapshot_file}", flush=True)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"Не удалось подготовить задания: {type(exc).__name__}")
        return 1
    except KeyboardInterrupt:
        print("Сбор остановлен пользователем")
        return 1
    except Exception as exc:
        print(f"Не удалось получить задания: {type(exc).__name__}. Проверьте сеть и локальный Secrets.")
        return 1
    try:
        run_id = save_snapshot(settings, snapshot)
    except Exception as exc:
        print(f"Сохранение в Supabase не подтверждено: {type(exc).__name__}.")
        print(f'Повтор без повторного сбора: python -m price_monitor.sensoren_local --resume "{snapshot_file}"')
        return 1
    print(f"Сохранён запуск №{run_id}. В Streamlit откройте Результаты и нажмите Обновить результаты.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
