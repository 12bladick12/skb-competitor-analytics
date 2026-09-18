from __future__ import annotations

from .models import ClassificationResult
from .utils import normalize_text


CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "новая продукция": [
        "новый",
        "новинка",
        "новая серия",
        "новая продукция",
        "расширение линейки",
        "теперь доступно",
        "добавили",
        "поступление",
        "запуск производства",
    ],
    "акция": ["акция", "скидка", "спецпредложение", "распродажа", "снижена цена", "промо"],
    "технический материал": [
        "паспорт",
        "руководство",
        "инструкция",
        "каталог",
        "datasheet",
        "техническое описание",
        "сертификат",
        "схема подключения",
    ],
    "изменение позиционирования": [
        "импортозамещение",
        "российское производство",
        "аналог",
        "замена",
        "собственное производство",
        "склад",
        "наличие",
        "быстрые сроки",
        "индивидуальное исполнение",
    ],
    "коммерческое предложение": ["цена", "стоимость", "поставка", "коммерческое предложение", "заказать", "купить"],
    "новость": ["новости", "сообщаем", "выставка", "участие", "обновление", "анонс"],
}


class RuleBasedClassifier:
    def classify(self, title: str, text: str, source_type: str = "") -> ClassificationResult:
        haystack = normalize_text(f"{title} {text}").lower()
        scores: list[tuple[str, list[str]]] = []
        for category, keywords in CATEGORY_KEYWORDS.items():
            matched = [word for word in keywords if word in haystack]
            if matched:
                scores.append((category, matched))

        if source_type == "telegram":
            base = ClassificationResult(category="Telegram-публикация", confidence=0.75, matched_keywords=["telegram"], short_summary=self.summarize(text))
            if not scores:
                return base

        if not scores:
            if source_type == "news":
                return ClassificationResult(category="новость", confidence=0.6, matched_keywords=["source:news"], short_summary=self.summarize(text))
            if source_type == "promotions":
                return ClassificationResult(category="акция", confidence=0.6, matched_keywords=["source:promotions"], short_summary=self.summarize(text))
            if source_type == "docs":
                return ClassificationResult(category="технический материал", confidence=0.6, matched_keywords=["source:docs"], short_summary=self.summarize(text))
            return ClassificationResult(category="прочее", confidence=0.2, matched_keywords=[], short_summary=self.summarize(text))

        scores.sort(key=lambda item: len(item[1]), reverse=True)
        category, matched = scores[0]
        confidence = min(0.95, 0.45 + len(matched) * 0.12)
        return ClassificationResult(category=category, confidence=confidence, matched_keywords=matched, short_summary=self.summarize(text))

    def summarize(self, text: str, max_chars: int = 320) -> str:
        clean = normalize_text(text)
        if len(clean) <= max_chars:
            return clean
        boundary = clean.rfind(".", 0, max_chars)
        return clean[: boundary + 1 if boundary > 80 else max_chars].strip()
