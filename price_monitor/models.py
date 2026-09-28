from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from urllib.parse import quote


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    # Keep meaningful punctuation and Cyrillic/Latin differences in model names.
    value = unicodedata.normalize("NFKC", value).upper().strip()
    value = value.translate(str.maketrans({"–": "-", "—": "-", "‑": "-"}))
    return re.sub(r"\s+", " ", value)


@dataclass(frozen=True)
class Rule:
    source: str
    manufacturer: str
    article: str
    product_url: str = ""
    url_template: str = ""

    @property
    def url(self) -> str:
        return self.product_url or self.url_template.replace("{article}", quote(self.article, safe=""))

    @property
    def key(self) -> str:
        values = [self.source, normalize(self.manufacturer), normalize(self.article), self.url]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Observation:
    status: str
    url: str
    title: str = ""
    price: str | None = None
    currency: str | None = None
    availability: str = "unknown"
    price_text: str = ""
    availability_text: str = ""
    detail: str = ""
    checked_at: str = ""
    http_status: int | None = None
    response_hash: str = ""
    details_json: str = "{}"

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = utcnow()


STATUS_LABELS = {
    "priced": "Цена получена", "on_request": "Цена по запросу", "no_price": "Цена не опубликована",
    "not_found": "Товар не найден", "identity_mismatch": "Не совпала модель", "needs_variant": "Уточните исполнение",
    "blocked": "Доступ ограничен", "robots_denied": "Запрещено robots.txt", "robots_unavailable": "Не удалось проверить robots.txt",
    "rate_limited": "Лимит запросов", "source_stopped": "Источник остановлен", "network_error": "Сетевая ошибка",
    "http_error": "Ошибка HTTP", "parse_error": "Изменилась страница", "cancelled": "Отменено",
    "pending": "В очереди", "processing": "Обрабатывается",
}
AVAILABILITY_LABELS = {
    "in_stock": "В наличии", "out_of_stock": "Нет в наличии", "on_order": "Под заказ",
    "discontinued": "Снят с производства", "unknown": "Не указано",
}
RUN_LABELS = {"queued": "В очереди", "running": "Выполняется", "completed": "Завершён", "completed_with_errors": "Завершён с замечаниями", "cancelled": "Остановлен", "failed": "Ошибка сборщика"}
SUCCESS_STATUSES = {"priced", "on_request", "no_price", "not_found"}
