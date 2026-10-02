"""Observed price changes, preserving historical offer conditions and evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json

from .price_terms import terms_for
from .scope import VISIBLE


@dataclass
class ChangeReport:
    events: list[dict] = field(default_factory=list)
    compared_pairs: int = 0
    incompatible_pairs: int = 0
    observations_in_period: int = 0
    models_in_period: int = 0
    models_with_history: int = 0
    last_checked_at: str | None = None


def _timestamp(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _price(value):
    try:
        number = Decimal(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _signature(row, terms):
    currency = str(row.get("currency") or "").strip().upper()
    basis, unit = terms.get("basis"), terms.get("unit")
    if not currency or basis not in ("gross", "net") or not unit:
        return None
    rate = terms.get("rate")
    if rate is not None:
        try:
            rate = Decimal(str(rate))
            if not rate.is_finite() or not 0 <= rate <= 100:
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
    return currency, basis, unit, rate


def calculate_price_changes(observations, start=None, end=None):
    """Compare consecutive valid quotes; end is exclusive and dates are UTC.

    Include observations before start so that the first quote within the selected
    period is compared with its actual predecessor. Failures and missing prices
    never become a zero quote. A change of offer conditions starts a new baseline.
    """
    start_at, end_at = _timestamp(start), _timestamp(end)
    report = ChangeReport()
    valid = []
    for original in observations:
        row = dict(original)
        checked_at, price = _timestamp(row.get("checked_at")), _price(row.get("price"))
        if row.get("status") != "priced" or checked_at is None or price is None:
            continue
        if end_at is not None and checked_at >= end_at:
            continue
        if "_specifications" not in row:
            row["_specifications"] = _json_object(row.get("details_json"))
        valid.append((checked_at, int(row.get("id") or 0), price, row))
    valid.sort(key=lambda record: (record[0], record[1]))
    previous = {}
    for checked_at, observation_id, price, row in valid:
        # Keep different product cards and collectors separate even if a model
        # designation is shared. Terms/currency are checked between adjacent quotes.
        identity = (row.get("rule_id"), row.get("source"), row.get("manufacturer"),
                    row.get("article"), row.get("product_url") or row.get("url"))
        terms = terms_for(row)
        signature = _signature(row, terms)
        old = previous.get(identity)
        previous[identity] = (checked_at, price, row, signature)
        if (start_at is not None and checked_at < start_at) or old is None:
            continue
        old_at, old_price, old_row, old_signature = old
        if signature is None or signature != old_signature:
            report.incompatible_pairs += 1
            continue
        report.compared_pairs += 1
        if price == old_price:
            continue
        difference = abs(price - old_price)
        report.events.append({
            "rule_id": row.get("rule_id"), "observation_id": observation_id,
            "previous_observation_id": old_row.get("id"),
            "checked_at": checked_at.isoformat(), "previous_checked_at": old_at.isoformat(),
            "source": row.get("source", ""), "manufacturer": row.get("manufacturer", ""),
            "article": row.get("article", ""),
            "previous_price": float(old_price), "price": float(price),
            "change": float(difference), "change_percent": float(difference / old_price * 100),
            "direction": "increase" if price > old_price else "decrease",
            "currency": signature[0], "tax_basis": signature[1], "unit": signature[2],
            "vat_rate": float(signature[3]) if signature[3] is not None else None,
            "url": row.get("url") or row.get("product_url") or "",
            "previous_url": old_row.get("url") or old_row.get("product_url") or "",
        })
    report.events.sort(key=lambda row: (row["checked_at"], row["observation_id"]), reverse=True)
    return report


def _filters(manufacturer="", query=""):
    conditions, params = [VISIBLE], {}
    if manufacturer:
        conditions.append("q.manufacturer=%(manufacturer)s")
        params["manufacturer"] = manufacturer
    for index, word in enumerate(query.casefold().split()[:8]):
        key = f"word{index}"
        conditions.append(f"lower(q.article||' '||q.manufacturer) LIKE %({key})s ESCAPE '!'")
        params[key] = "%" + word.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
    return conditions, params


def history_overview(library):
    rows = library.repo.batch(f"""SELECT q.manufacturer,min(o.checked_at) first_checked_at,
        max(o.checked_at) last_checked_at,count(*) observations
        FROM rules q JOIN jobs j ON j.rule_id=q.id JOIN observations o ON o.job_id=j.id
        WHERE {VISIBLE} AND o.status='priced' AND o.price IS NOT NULL
        GROUP BY q.manufacturer ORDER BY q.manufacturer""")
    return rows


def _attach_snapshot_terms(library, rows):
    """Read only documents referenced by these quotes, never current card terms."""
    references = set()
    for row in rows:
        payload = _json_object(row.get("details_json"))
        row["_specifications"] = payload
        if payload.get("ref"):
            references.add(payload["ref"])
    references = sorted(references)
    documents = {}
    for offset in range(0, len(references), 100):
        params = {f"ref{i}": value for i, value in enumerate(references[offset:offset + 100])}
        # Project the small conditions object; full specifications can contain
        # lengthy descriptions and are irrelevant to a historical price comparison.
        projection = ("(details_json::jsonb->'price_terms')::text" if library.repo.settings
                      else "json_extract(details_json,'$.price_terms')")
        found = library.repo.batch(
            "SELECT fingerprint," + projection + " price_terms FROM product_documents WHERE fingerprint IN ("
            + ",".join(f"%({key})s" for key in params) + ")", params)
        for document in found:
            documents[document["fingerprint"]] = {"price_terms": _json_object(document.get("price_terms"))}
    for row in rows:
        reference = row["_specifications"].get("ref")
        if reference:
            row["_specifications"] = documents.get(reference, {})
    return rows


def load_price_changes(library, start, end, manufacturer="", query=""):
    """Read a bounded set of candidate models and their historical snapshots.

    A one-quote model cannot have a price change, so its large product document is
    never downloaded. No write, current-price override or synthetic quote is used.
    """
    start_at, end_at = _timestamp(start), _timestamp(end)
    if start_at is None or end_at is None or start_at >= end_at:
        raise ValueError("Начало периода должно быть раньше конца периода")
    conditions, params = _filters(manufacturer, query)
    params.update(start=start_at.date().isoformat() if start_at.time() == time.min else start_at.isoformat(),
                  end=end_at.date().isoformat() if end_at.time() == time.min else end_at.isoformat())
    from_sql = " FROM rules q JOIN jobs j ON j.rule_id=q.id JOIN observations o ON o.job_id=j.id WHERE "
    common = conditions + ["o.status='priced'", "o.price IS NOT NULL", "o.checked_at<%(end)s"]
    stats = library.repo.batch("SELECT count(*) observations,count(DISTINCT q.id) models,max(o.checked_at) last_checked_at"
                              + from_sql + " AND ".join(common + ["o.checked_at>=%(start)s"]), params)[0]
    report = ChangeReport(observations_in_period=stats["observations"], models_in_period=stats["models"],
                          last_checked_at=stats["last_checked_at"])
    candidates = library.repo.batch("SELECT q.id" + from_sql + " AND ".join(common)
                                    + " GROUP BY q.id HAVING count(*)>=2 AND max(o.checked_at)>=%(start)s ORDER BY q.id", params)
    report.models_with_history = len(candidates)
    for offset in range(0, len(candidates), 100):
        ids = [row["id"] for row in candidates[offset:offset + 100]]
        # No start bound: the latest successful quote before the period is needed.
        rows = [dict(row) for row in library.history(ids, end=params["end"]) if row.get("status") == "priced"]
        result = calculate_price_changes(_attach_snapshot_terms(library, rows), start_at, end_at)
        report.events.extend(result.events)
        report.compared_pairs += result.compared_pairs
        report.incompatible_pairs += result.incompatible_pairs
    report.events.sort(key=lambda row: (row["checked_at"], row["observation_id"]), reverse=True)
    return report


def change_table(events):
    from .product_labels import display_article
    from .sources import SOURCES

    result = []
    for row in events:
        source = SOURCES.get(row["source"])
        result.append({
            "Дата изменения (UTC)": row["checked_at"],
            "Производитель": row["manufacturer"], "Маркировка": display_article(row),
            "Предыдущая цена": row["previous_price"], "Новая цена": row["price"],
            "Величина изменения": row["change"], "Изменение, %": row["change_percent"],
            "Направление": "Рост" if row["direction"] == "increase" else "Снижение",
            "Валюта": row["currency"],
            "Единица цены": {"piece": "шт.", "pack": "упаковка / комплект"}.get(row["unit"], row["unit"]),
            "НДС": "С НДС" if row["tax_basis"] == "gross" else "Без НДС", "Ставка НДС, %": row["vat_rate"],
            "Источник": source.label if source else row["source"], "Ссылка": row["url"],
            "Предыдущий замер (UTC)": row["previous_checked_at"], "Предыдущая ссылка": row["previous_url"],
            "ID наблюдения": row["observation_id"], "ID предыдущего наблюдения": row["previous_observation_id"],
        })
    return result
