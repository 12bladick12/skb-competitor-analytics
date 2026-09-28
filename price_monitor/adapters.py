"""Selectors are scoped to the main offer, never recommendations or the cart."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re

from bs4 import BeautifulSoup

from .models import Observation, Rule, normalize
from .sources import SOURCES


def node_text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def contains_article(text: str, article: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(normalize(article)) + r"(?![\w-])", normalize(text)))


def parse_money(value: str) -> str | None:
    # A single amount only. Never concatenate price + VAT percentage / units.
    text = value.replace("\xa0", " ").replace("\u202f", " ").strip()
    match = re.fullmatch(r"([0-9]+(?: [0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(?:₽|руб\.?|RUB|EUR|USD|€|\$)?(?:\s*/\s*шт\.?)?", text, re.I)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(" ", "").replace(",", "."))
        return format(amount.quantize(Decimal("0.01")), "f") if amount > 0 else None
    except InvalidOperation:
        return None


def availability(text: str) -> str:
    t = text.lower()
    if "снят с производства" in t or "снято с производства" in t:
        return "discontinued"
    if any(w in t for w in ("нет в наличии", "нет на складе", "outofstock")):
        return "out_of_stock"
    if any(w in t for w in ("под заказ", "предзаказ", "preorder", "backorder")):
        return "on_order"
    if any(w in t for w in ("в наличии", "instock")):
        return "in_stock"
    return "unknown"


class Adapter:
    version = "1.0"

    def __init__(self, source: str):
        self.spec = SOURCES[source]

    def parse(self, rule: Rule, html: str, url: str | None = None, http_status: int = 200) -> Observation:
        result = Observation("parse_error", url or rule.url, http_status=http_status,
                             response_hash=hashlib.sha256(html.encode("utf-8")).hexdigest())
        if http_status in (404, 410):
            result.status, result.detail = "not_found", f"HTTP {http_status}"
            return result
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1")
        result.title = node_text(h1)
        if re.search(r"(страница|товар).{0,20}не найден|page not found|ошибка 404", result.title, re.I):
            result.status, result.detail = "not_found", "Страница сообщает, что товар не найден"
            return result
        root = soup.select_one(self.spec.scope)
        if not h1 or not root:
            result.detail = "Не найден основной блок карточки; нужна проверка адаптера"
            return result
        if self.spec.id == "beskonta":
            selected = node_text(root.select_one(".rs-product-barcode"))
            has_choices = root.select_one("select, .rs-multioffers") is not None
            if selected and normalize(selected) != normalize(rule.article):
                result.status, result.detail = "needs_variant", f"В HTML выбрано исполнение {selected}; укажите его точную маркировку или ссылку на нужное исполнение"
                return result
            if has_choices and not selected:
                result.status, result.detail = "needs_variant", "На странице есть варианты, выбранная маркировка не подтверждена"
                return result
            identities = [result.title, selected]
        else:
            identities = [result.title]
            identities += [n.get("content") or node_text(n) for n in root.select('[itemprop="sku"], [itemprop="mpn"]')]
        if not any(contains_article(i, rule.article) for i in identities if i):
            result.status, result.detail = "identity_mismatch", "Артикул не подтверждён заголовком или маркировкой карточки"
            return result
        if self.spec.id == "sensoren":
            brand = re.sub(r"[^\w]", "", normalize(rule.manufacturer))
            title_brand = re.sub(r"[^\w]", "", normalize(result.title))
            if brand not in title_brand:
                result.status, result.detail = "identity_mismatch", "Производитель не подтверждён заголовком карточки Sensoren"
                return result
        av_nodes = root.select(self.spec.availability_selector)
        av_text = " ".join(node_text(n) for n in av_nodes)
        if not av_text and self.spec.id == "megak":
            av_text = " ".join(n.get("href", n.get("content", "")) for n in root.select('[itemprop="availability"]'))
        result.availability = availability(av_text)
        # Keep relevant evidence short, without copying whole specifications.
        av_match = re.search(r"(?:товар снят с производства|нет в наличии|нет на складе|есть в наличии|в наличии|под заказ[^.\n]{0,45}|срок поставки:[^.\n]{0,45})", av_text, re.I)
        result.availability_text = av_match.group(0) if av_match else ""
        if result.availability == "discontinued":
            result.status, result.detail = "no_price", "Товар снят с производства; цены рекомендованных замен исключены"
            return result
        price_nodes = root.select(self.spec.price_selector)
        prices = []
        texts = []
        for node in price_nodes:
            raw = node.get("content") or node_text(node)
            texts.append(raw)
            number = parse_money(raw)
            if number:
                prices.append(number)
        result.price_text = " | ".join(dict.fromkeys(texts))[:500]
        if len(set(prices)) > 1:
            result.detail = "В основном блоке несколько разных цен; требуется проверка исполнения"
            return result
        if prices:
            currency_node = root.select_one('[itemprop="priceCurrency"]')
            curr = (currency_node.get("content") or node_text(currency_node)).upper() if currency_node else ""
            curr = curr or ("RUB" if re.search(r"₽|руб|RUB", result.price_text, re.I) else "")
            # BESKONTA keeps the symbol in a sibling node.
            if not curr:
                currency_sibling = root.select_one(".p-p-price-currency")
                if node_text(currency_sibling) == "₽":
                    curr = "RUB"
            if curr not in {"RUB", "USD", "EUR", "CNY"}:
                result.detail = "Число найдено, но валюта не подтверждена"
                return result
            result.status, result.price, result.currency = "priced", prices[0], curr
            return result
        # Only the offer area may declare a price on request.
        offer_scope = {
            "sensoren": ".product-info__all-order", "beskonta": ".p-p-block-price", "megak": ".details-payment",
            "teko": ".catalog-detail-right", "sensor": ".navigation-product__priceblock",
        }[self.spec.id]
        offer = root.select_one(offer_scope)
        offer_text = node_text(offer or root) if self.spec.id == "beskonta" else node_text(offer)
        price_block = {"sensoren": ".product-info__all-order-price", "beskonta": ".p-p-block-price", "megak": ".details-payment-price", "teko": ".catalog-detail-price", "sensor": ".navigation-product__priceblock"}[self.spec.id]
        price_block_text = node_text(root.select_one(price_block))
        request_match = re.search(r"(?:цен[ау]|стоимость)\s*(?:по запросу|уточня[а-я]*)|(?:уточнить|уточняйте|запросить)\s*(?:цену|стоимость)", offer_text, re.I)
        if request_match:
            result.status, result.price_text = "on_request", request_match.group(0)
        elif not price_nodes and re.search(r"\d[\d\s.,]*\s*(?:₽|руб\b|RUB)", price_block_text, re.I):
            result.status, result.detail = "parse_error", "В блоке предложения есть цена, но селектор изменился"
        elif price_nodes and any(t.strip() for t in texts) and not all(re.fullmatch(r"0(?:[.,]0+)?\s*(?:₽|руб\.?)?", t.strip()) for t in texts):
            result.status, result.detail = "parse_error", "Блок цены найден, но формат не распознан"
        else:
            result.status, result.detail = "no_price", "В открытом HTML основной карточки нет опубликованной цены"
        return result


ADAPTERS = {source: Adapter(source) for source in SOURCES}
