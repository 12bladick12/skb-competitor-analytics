"""Validated, versioned import of externally researched public evidence.

The application does not crawl the web here. A trusted operator supplies a JSON
bundle after research; each import is one atomic write to the active library.
"""

from datetime import date, datetime, timedelta, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from .drive_store import StorageError
from .library import Repository, canonical


MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 500
PERIOD = re.compile(r"(?!0000)\d{4}-(?:0[1-9]|1[0-2])\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class _VisibleText(HTMLParser):
    """Extract text shown by a saved HTML page, without scripts or markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def _hidden(self):
        return bool(self.stack and self.stack[-1][1])

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = (attrs.get("style") or "").casefold()
        concealed = (tag in ("script", "style", "noscript", "template") or "hidden" in attrs
                     or (attrs.get("aria-hidden") or "").strip().casefold() == "true"
                     or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", style)))
        hidden = self._hidden() or concealed
        if not hidden and tag in ("p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append(" ")
        if tag not in ("br", "hr", "img", "meta", "link", "input", "source", "wbr"):
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if not self._hidden() and tag in ("p", "div", "li", "tr", "td", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._hidden():
            self.parts.append(data)


def _visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(unescape("".join(parser.parts)).split())


def _url_identity(value):
    parts = urlsplit(value)
    port = parts.port
    host = parts.hostname.lower()
    authority = host if port in (None, 443) else f"{host}:{port}"
    return authority, parts.path or "/", parts.query


def _text(value, name, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (required and not value.strip()):
        raise ValueError(f"Некорректное поле исследования: {name}.")
    return value.strip()


def _instant(value, name):
    raw = _text(value, name, 40)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError(f"Некорректная дата и время: {name}.") from None
    return parsed.isoformat()


def _url(value, name, *, required=True):
    raw = _text(value, name, 2048, required=required)
    if not raw:
        return raw
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError(f"Нужна прямая HTTPS-ссылка: {name}.")
    return raw


def _date(item):
    precision = item.get("published_precision")
    if precision not in ("day", "month", "unknown"):
        raise ValueError("Укажите точность даты: день, месяц или неизвестна.")
    value = item.get("published_at")
    if precision == "unknown":
        if value is not None or item.get("period") is not None:
            raise ValueError("Неизвестную дату нельзя относить к месяцу.")
        return None, None
    if not isinstance(value, str):
        raise ValueError("Укажите подтверждённую дату.")
    if precision == "month":
        if not PERIOD.fullmatch(value):
            raise ValueError("Месяц должен иметь вид ГГГГ-ММ.")
        period = value
    else:
        try:
            if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("День должен иметь вид ГГГГ-ММ-ДД.") from None
        period = value[:7]
    if item.get("period", period) != period:
        raise ValueError("Период материала не совпадает с доказанной датой.")
    return value, period


def _same_candidate(candidate, current):
    return (candidate.get("status") == "candidate"
            and candidate.get("category") == "arbitration"
            and candidate.get("competitor_code") == current["competitor_code"]
            and candidate.get("case_number") == current["case_number"]
            and candidate.get("case_url") == current["case_url"]
            and candidate.get("published_at") == current["published_at"]
            and candidate.get("published_precision") == current["published_precision"]
            and candidate.get("source_url") == current["source_url"])


def validate_bundle(bundle, competitors, existing_items=None):
    """Return records ready for insertion; never infer completeness or dates."""
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("Неподдерживаемая версия файла исследования.")
    if len(canonical(bundle).encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ValueError("Файл исследования слишком велик.")
    run = bundle.get("run")
    items = bundle.get("items")
    if not isinstance(run, dict) or not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("Некорректный состав файла исследования.")
    period = run.get("period")
    category = run.get("category")
    status = run.get("status")
    if not isinstance(period, str) or not PERIOD.fullmatch(period):
        raise ValueError("Укажите период исследования ГГГГ-ММ.")
    if category not in ("arbitration", "media") or status not in ("success", "partial", "error"):
        raise ValueError("Некорректные категория или результат исследования.")
    started = _instant(run.get("started_at"), "started_at")
    finished = _instant(run.get("finished_at"), "finished_at")
    started_at = datetime.fromisoformat(started)
    finished_at = datetime.fromisoformat(finished)
    if finished_at < started_at:
        raise ValueError("Исследование завершилось раньше начала.")
    if finished_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Дата завершения исследования находится в будущем.")
    coverage = run.get("coverage")
    limitations = run.get("limitations")
    if not isinstance(coverage, list) or len(coverage) > 500 or not isinstance(limitations, list) or len(limitations) > 100:
        raise ValueError("Некорректное описание полноты исследования.")
    if status == "success" and (not coverage or limitations
                                or any(c.get("status") != "success" for c in coverage if isinstance(c, dict))):
        raise ValueError("Успешный обход требует подтверждённых проверок источников.")
    checked = []
    for entry in coverage:
        if not isinstance(entry, dict) or entry.get("status") not in ("success", "partial", "error", "blocked", "not_checked"):
            raise ValueError("Некорректная проверка источника.")
        checked.append({"source": _text(entry.get("source"), "coverage.source", 200),
                        "status": entry["status"],
                        "detail": _text(entry.get("detail", ""), "coverage.detail", 1000, required=False)})
    notes = [_text(note, "limitations", 1000) for note in limitations]
    normalized_run = {"period": period, "category": category, "status": status,
                      "started_at": started, "finished_at": finished,
                      "coverage": checked, "limitations": notes}
    normalized_run["id"] = hashlib.sha256(canonical(normalized_run).encode("utf-8")).hexdigest()
    records = [{"kind": "research_run", "key": normalized_run["id"], "payload": normalized_run}]
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get("category") != category:
            raise ValueError("Категория материала не совпадает с исследованием.")
        competitor = _text(item.get("competitor_code"), "competitor_code", 100)
        if competitor not in competitors:
            raise ValueError("Конкурент отсутствует в активном реестре.")
        published, item_period = _date(item)
        source_url = _url(item.get("source_url"), "source_url")
        proof = item.get("evidence")
        if not isinstance(proof, dict):
            raise ValueError("Укажите доказательство материала.")
        proof_url = _url(proof.get("url"), "evidence.url")
        quote = _text(proof.get("quote", ""), "evidence.quote", 5000, required=False)
        captured = _instant(proof.get("captured_at"), "evidence.captured_at")
        captured_at = datetime.fromisoformat(captured)
        discovered = _instant(item.get("discovered_at"), "discovered_at")
        discovered_at = datetime.fromisoformat(discovered)
        if not started_at <= discovered_at <= finished_at or captured_at > finished_at:
            raise ValueError("Дата обнаружения или снимка выходит за пределы исследования.")
        snapshot = proof.get("snapshot_sha256")
        if snapshot is not None and (not isinstance(snapshot, str) or not DIGEST.fullmatch(snapshot)):
            raise ValueError("Некорректная контрольная сумма снимка.")
        snapshot_html = proof.get("snapshot_html")
        if snapshot_html is not None:
            if (not isinstance(snapshot_html, str) or not snapshot_html
                    or len(snapshot_html) > 200000 or "\x00" in snapshot_html):
                raise ValueError("Некорректный HTML-снимок.")
            calculated = hashlib.sha256(snapshot_html.encode("utf-8")).hexdigest()
            if snapshot is not None and snapshot != calculated:
                raise ValueError("Контрольная сумма HTML-снимка не совпадает.")
            snapshot = calculated
        elif snapshot is not None:
            raise ValueError("Контрольная сумма без HTML-снимка не доказывает сохранение страницы.")
        requested_status = item.get("status")
        event_key = _text(item.get("event_key", ""), "event_key", 300, required=False)
        if category != "arbitration" and event_key:
            raise ValueError("Ключ события допустим только для арбитражного дела.")
        if event_key and len(event_key) < 4:
            raise ValueError("Ключ события слишком короткий для надёжной привязки.")
        verification = ("candidate" if category == "arbitration" and not event_key
                        else requested_status)
        if requested_status not in ("candidate", "verified") or (verification == "verified" and not quote):
            raise ValueError("Подтверждённый материал требует цитату из первоисточника.")
        if verification == "verified":
            if _url_identity(source_url) != _url_identity(proof_url):
                raise ValueError("Ссылка доказательства не совпадает с первоисточником.")
            if snapshot_html is None:
                raise ValueError("Подтверждённому материалу нужен сохранённый HTML-снимок.")
            if " ".join(unescape(quote).split()) not in _visible_text(snapshot_html):
                raise ValueError("Цитата отсутствует в сохранённом HTML-снимке.")
            if category == "arbitration" and event_key not in unescape(snapshot_html) and event_key not in source_url:
                raise ValueError("Ключ судебного события отсутствует в первоисточнике.")
        original = _text(item.get("original_text"), "original_text", 50000)
        case_number = _text(item.get("case_number", ""), "case_number", 100, required=False)
        case_url = _url(item.get("case_url", ""), "case_url", required=False)
        if category == "arbitration" and not (case_number or case_url):
            raise ValueError("Для судебного материала нужен номер дела или ссылка на карточку.")
        if (category == "arbitration" and event_key
                and (event_key.casefold() == case_number.casefold()
                     or (case_url and event_key == case_url))):
            raise ValueError("Ключ судебного события должен обозначать документ или событие, а не всё дело.")
        normalized = {"category": category, "competitor_code": competitor,
                      "title": _text(item.get("title"), "title", 500),
                      "summary": _text(item.get("summary", ""), "summary", 5000, required=False),
                      "original_text": original, "source_name": _text(item.get("source_name"), "source_name", 200),
                      "source_url": source_url, "published_at": published,
                      "published_precision": item["published_precision"], "period": item_period,
                      "date_basis": _text(item.get("date_basis", ""), "date_basis", 1000, required=False),
                      "discovered_at": discovered,
                      "status": verification,
                      "evidence": {"url": proof_url, "quote": quote, "captured_at": captured,
                                   "snapshot_sha256": snapshot, "snapshot_html": snapshot_html},
                      "case_number": case_number, "case_url": case_url, "event_key": event_key,
                      "run_id": normalized_run["id"]}
        if item_period and not normalized["date_basis"]:
            raise ValueError("Основание даты обязательно для материала с датой.")
        # A media article has one URL. A court event needs its own source key;
        # unkeyed observations remain candidates with a content fingerprint.
        subject = {"category": category, "competitor_code": competitor,
                   "source": case_number.casefold() if category == "arbitration" and case_number else source_url}
        if category == "arbitration":
            if event_key:
                subject.update(event_key=event_key)
            else:
                fingerprint = {"title": normalized["title"], "original_text": original,
                               "quote": quote, "source_url": source_url, "published_at": published}
                subject.update(candidate_fingerprint=hashlib.sha256(canonical(fingerprint).encode("utf-8")).hexdigest())
        normalized["id"] = hashlib.sha256(canonical(subject).encode("utf-8")).hexdigest()
        requested_supersession = item.get("supersedes_candidate_id")
        if requested_supersession is not None and (not isinstance(requested_supersession, str)
                                                    or not DIGEST.fullmatch(requested_supersession)):
            raise ValueError("Некорректный идентификатор заменяемого кандидата.")
        if requested_supersession is not None and (category != "arbitration" or verification != "verified"):
            raise ValueError("Связь с кандидатом допустима только для подтверждённого судебного события.")
        if category == "arbitration" and verification == "verified" and existing_items is not None:
            candidates = {key: old for key, old in existing_items.items()
                          if (isinstance(old, dict) and _same_candidate(old, normalized)
                              and old.get("superseded_by") in (None, normalized["id"]))}
            if requested_supersession is None:
                fingerprint = {"title": normalized["title"], "original_text": original,
                               "quote": quote, "source_url": source_url, "published_at": published}
                old_subject = {"category": category, "competitor_code": competitor,
                               "source": case_number.casefold() if case_number else source_url,
                               "candidate_fingerprint": hashlib.sha256(canonical(fingerprint).encode("utf-8")).hexdigest()}
                inferred = hashlib.sha256(canonical(old_subject).encode("utf-8")).hexdigest()
                if inferred in candidates:
                    requested_supersession = inferred
                elif len(already_linked := [key for key, old in candidates.items()
                                             if old.get("superseded_by") == normalized["id"]]) == 1:
                    requested_supersession = already_linked[0]
                elif candidates:
                    raise ValueError("Укажите supersedes_candidate_id для ранее найденного судебного события.")
            if requested_supersession is not None:
                if requested_supersession not in candidates or requested_supersession == normalized["id"]:
                    raise ValueError("Кандидат для замены отсутствует или относится к другому событию.")
                normalized["supersedes_candidate_id"] = requested_supersession
        elif requested_supersession is not None:
            raise ValueError("Для проверки замены нужен текущий реестр кандидатов.")
        if normalized["id"] in seen:
            raise ValueError("Одна статья или событие дела повторены в файле исследования.")
        seen.add(normalized["id"])
        content = {key: value for key, value in normalized.items() if key not in ("discovered_at", "run_id", "version")}
        normalized["version"] = hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()
        records.append({"kind": "research_item_version", "key": normalized["version"], "payload": normalized})
        records.append({"kind": "research_item", "key": normalized["id"], "payload": normalized})
    return records


class ResearchStore:
    def __init__(self, config, repository=None):
        self.repository = repository or Repository(config)

    def import_bundle(self, bundle):
        """Trusted backend operation. Database credentials authorize this write."""
        library = self.repository.load()
        if not library:
            raise StorageError("Активная библиотека не найдена.")
        records = validate_bundle(bundle, library["competitor"], library.get("research_item", {}))
        from psycopg import sql
        from psycopg.types.json import Jsonb
        body = sql.SQL("""DECLARE active_id text; data jsonb := {records}; link jsonb;
          previous jsonb; promoted jsonb; replay_versions jsonb;
        BEGIN
          PERFORM pg_advisory_xact_lock(847263918);
          SELECT import_id INTO active_id FROM skb_analytics.state WHERE key='active' FOR SHARE;
          IF active_id IS DISTINCT FROM {import_id} THEN
            RAISE EXCEPTION 'Active library changed';
          END IF;
          SELECT COALESCE(jsonb_agg(v.key),'[]'::jsonb) INTO replay_versions
            FROM skb_analytics.records v WHERE v.import_id=active_id
              AND v.kind='research_item_version' AND v.key IN (
                SELECT r.key FROM jsonb_to_recordset(data)
                  AS r(kind text,key text,payload jsonb) WHERE r.kind='research_item_version');
          INSERT INTO skb_analytics.records(import_id,kind,key,payload)
            SELECT active_id,r.kind,r.key,r.payload
            FROM jsonb_to_recordset(data) AS r(kind text,key text,payload jsonb)
            WHERE r.kind IN ('research_run','research_item_version')
            ON CONFLICT (import_id,kind,key) DO NOTHING;
          INSERT INTO skb_analytics.records(import_id,kind,key,payload)
            SELECT active_id,r.kind,r.key,r.payload
            FROM jsonb_to_recordset(data) AS r(kind text,key text,payload jsonb)
            WHERE r.kind='research_item'
            ON CONFLICT (import_id,kind,key) DO UPDATE SET payload=excluded.payload
            WHERE (skb_analytics.records.payload->>'discovered_at')::timestamptz
                  < (excluded.payload->>'discovered_at')::timestamptz
              AND (skb_analytics.records.payload->>'status'<>'verified'
                   OR excluded.payload->>'status'='verified')
              AND (skb_analytics.records.payload->>'status'<>'candidate'
                   OR NOT (skb_analytics.records.payload ? 'superseded_by'));
          FOR link IN SELECT r.payload FROM jsonb_to_recordset(data)
              AS r(kind text,key text,payload jsonb)
              WHERE r.kind='research_item' AND r.payload ? 'supersedes_candidate_id' LOOP
            SELECT payload INTO promoted FROM skb_analytics.records
              WHERE import_id=active_id AND kind='research_item' AND key=link->>'id' FOR UPDATE;
            IF NOT FOUND OR promoted->>'status'<>'verified' THEN
              RAISE EXCEPTION 'Verified observation changed';
            END IF;
            SELECT payload INTO previous FROM skb_analytics.records
              WHERE import_id=active_id AND kind='research_item'
                AND key=link->>'supersedes_candidate_id' FOR UPDATE;
            IF NOT FOUND OR previous->>'status'<>'candidate'
                OR previous->>'category'<>'arbitration'
                OR previous->'competitor_code' IS DISTINCT FROM link->'competitor_code'
                OR previous->'case_number' IS DISTINCT FROM link->'case_number'
                OR previous->'case_url' IS DISTINCT FROM link->'case_url'
                OR previous->'published_at' IS DISTINCT FROM link->'published_at'
                OR previous->'published_precision' IS DISTINCT FROM link->'published_precision'
                OR previous->'source_url' IS DISTINCT FROM link->'source_url'
                OR (previous ? 'superseded_by' AND previous->>'superseded_by'<>link->>'id') THEN
              RAISE EXCEPTION 'Candidate changed before promotion';
            END IF;
            IF promoted->>'version'<>link->>'version' THEN
              -- A previously committed version may be replayed after a newer
              -- revision. The linkage is already fixed; do not roll it back.
              IF previous->>'superseded_by'=link->>'id'
                  AND replay_versions ? (link->>'version') THEN
                CONTINUE;
              END IF;
              RAISE EXCEPTION 'Verified observation changed';
            END IF;
            UPDATE skb_analytics.records SET payload=payload ||
              jsonb_build_object('superseded_by',link->>'id')
              WHERE import_id=active_id AND kind='research_item'
                AND key=link->>'supersedes_candidate_id'
                AND NOT (payload ? 'superseded_by');
          END LOOP;
        END;""").format(records=sql.Literal(Jsonb(records, dumps=canonical)),
                         import_id=sql.Literal(library["id"]))
        try:
            with self.repository.write_connect(**self.repository.params) as conn:
                conn.execute(sql.SQL("DO {}").format(sql.Literal(body.as_string(conn))))
        except Exception:
            raise StorageError("Исследование не сохранено. Проверьте активную библиотеку и повторите импорт.") from None
        return {"run_id": records[0]["key"], "items": (len(records) - 1) // 2}


def import_json_file(config, path):
    """Operator entry point; caller supplies existing private server config."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError("Файл исследования слишком велик.")
        bundle = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Не удалось прочитать JSON-файл исследования.") from None
    return ResearchStore(config).import_bundle(bundle)


if __name__ == "__main__":
    import argparse
    from .readiness import load_secrets

    parser = argparse.ArgumentParser(description="Import externally researched public evidence")
    parser.add_argument("--secrets", required=True, help="Existing private Streamlit TOML settings")
    parser.add_argument("--bundle", required=True, help="Validated research JSON bundle")
    arguments = parser.parse_args()
    outcome = import_json_file(load_secrets(arguments.secrets), arguments.bundle)
    print(json.dumps(outcome, ensure_ascii=False))
