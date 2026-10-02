from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    host: str
    brands: tuple[str, ...]
    scope: str
    price_selector: str
    availability_selector: str
    path_prefix: str
    alternate_prefixes: tuple[str, ...] = ()

    @property
    def hosts(self):
        return {self.host, "www." + self.host}


SOURCES = {
    "sensoren": Source("sensoren", "Sensoren", "sensoren.ru", ("Autonics", "Balluff", "Pepperl+Fuchs", "ifm", "LANBAO", "SICK"), ".product-info", ".product-info__all-order-price .price", ".product-info__all", "/product/"),
    "beskonta": Source("beskonta", "BESKONTA", "beskonta.ru", ("BESKONTA",), ".p-p-info", ".p-p-price .rs-price-new", ".p-p-block-price", "/product/"),
    "megak": Source("megak", "МЕГА-К", "mega-k.com", ("МЕГА-К",), '[itemtype$="/Product"]', '.details-payment-price .price-current, [itemprop="offers"] [itemprop="price"]', ".details-availability", "/products/"),
    "teko": Source("teko", "ТЕКО", "teko-com.ru", ("ТЕКО",), ".catalog-detail", '.catalog-detail-price > div', ".catalog-detail-stock", "/product/", ("/catalog/product/",)),
    "sensor": Source("sensor", "СЕНСОР", "sensor-com.ru", ("СЕНСОР",), ".product-page__container", ".navigation-product__priceblock .navigation-product__price", ".navigation-product__priceblock", "/sensors/"),
}


def validate_url(source: str, url: str, product: bool = True) -> str:
    spec = SOURCES[source]
    if any(ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Недопустимые символы в URL")
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in spec.hosts or p.port not in (None, 443) or p.username or p.password:
        raise ValueError(f"Нужна HTTPS-ссылка на {spec.host} без логина и нестандартного порта")
    if p.fragment:
        raise ValueError("Удалите #фрагмент из ссылки")
    if product and not any(p.path.startswith(prefix) and p.path.rstrip("/") != prefix.rstrip("/") for prefix in (spec.path_prefix,)+spec.alternate_prefixes):
        raise ValueError(f"Нужна ссылка на карточку товара ({spec.path_prefix}…), а не поиск или категория")
    return url
