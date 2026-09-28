"""Small, reproducible public-page audit. Run explicitly; never a full crawler."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_monitor.robots import Robots
from price_monitor.sources import SOURCES, validate_url

OUT = ROOT / "docs" / "audit" / "raw"
TARGETS = {
    "sensoren": ["https://sensoren.ru/robots.txt", "https://sensoren.ru/sitemap.xml", "https://sensoren.ru/catalog/", "https://sensoren.ru/product/datchik_potoka_ifm_electronic_si5000/"],
    "beskonta": ["https://beskonta.ru/robots.txt", "https://beskonta.ru/sitemap-1.xml", "https://beskonta.ru/catalog/all/", "https://beskonta.ru/product/bore01-opticheskiy-fotobarer/"],
    "megak": ["https://mega-k.com/robots.txt", "https://mega-k.com/sitemap.xml", "https://mega-k.com/catalog"],
    "teko": ["https://teko-com.ru/robots.txt", "https://teko-com.ru/sitemap/sitemap.xml", "https://teko-com.ru/catalog/datchiki/", "https://teko-com.ru/product/cp-s254r-3-pg9.html"],
    "sensor": ["https://sensor-com.ru/robots.txt", "https://sensor-com.ru/sitemap/main.xml", "https://sensor-com.ru/", "https://sensor-com.ru/sensors/vbi-d06-45u-1111-z/"],
}


def audit(source, urls):
    results = []
    source_id = source.split("_", 1)[0]
    spec = SOURCES[source_id]
    if not urls[0].endswith("/robots.txt"):
        urls = [f"https://{spec.host}/robots.txt"] + list(urls)
    policy = None
    with requests.Session() as session:
        session.headers["User-Agent"] = "PriceMonitorAudit/1.0"
        for n, url in enumerate(urls):
            start = time.monotonic()
            item = {"source": source, "requested_url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
            try:
                target = url
                is_robots = url.endswith("/robots.txt")
                for redirect_number in range(5):
                    validate_url(source_id, target, product=False)
                    if not is_robots and (policy is None or not policy.allows(target)):
                        raise ValueError("URL not permitted by verified robots.txt")
                    r = session.get(target, timeout=(10, 25), allow_redirects=False)
                    if r.status_code not in (301,302,303,307,308):
                        break
                    target = urljoin(target, r.headers.get("Location", ""))
                    parts = urlsplit(target)
                    if parts.scheme == "http" and parts.hostname in spec.hosts and parts.port in (None,80) and not parts.username:
                        target = urlunsplit(("https",parts.hostname,parts.path,parts.query,parts.fragment))
                    time.sleep(max(2, policy.delay if policy else 0))
                else:
                    raise ValueError("Too many redirects")
                if r.encoding is None or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding
                body = r.text
                if is_robots:
                    if r.status_code == 200 and not re.search(r"<html|<!doctype html",body[:1000],re.I):
                        policy = Robots(body, agent="PriceMonitorAudit")
                    elif r.status_code in (404,410):
                        policy = Robots("")
                filename = f"{source}_{n}.txt"
                (OUT / filename).write_text(body, encoding="utf-8")
                item.update(status=r.status_code, final_url=r.url, seconds=round(time.monotonic()-start, 2), bytes=len(r.content), content_type=r.headers.get("Content-Type"), sha256=hashlib.sha256(r.content).hexdigest(), file=filename)
                # XML/robots are retained as evidence without interpreting them as HTML.
                soup = BeautifulSoup(body if "html" in r.headers.get("Content-Type", "") and not is_robots else "", "html.parser")
                item["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
                item["h1"] = [x.get_text(" ", strip=True) for x in soup.select("h1")]
                item["forms"] = [{"action": x.get("action"), "method": x.get("method"), "fields": [i.get("name") for i in x.select("input[name]")]} for x in soup.select("form")][:10]
                item["price_nodes"] = [str(x)[:1600] for x in soup.select('[itemprop="price"], [itemprop="priceCurrency"], [itemprop="availability"], [class*="price"]')][:30]
                item["product_links"] = list(dict.fromkeys(urljoin(r.url,x["href"]) for x in soup.select("a[href]") if any(p in x["href"] for p in ["/product/", "/products/", "/sensors/"])))[:12]
                if r.status_code in (401,403,429):
                    results.append(item)
                    break
            except (requests.RequestException, ValueError) as e:
                item["error"] = str(e)
            results.append(item)
            time.sleep(max(2, policy.delay if policy else 0))
    return results


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        groups = list(pool.map(lambda pair: audit(*pair), TARGETS.items()))
    summary = [item for group in groups for item in group]
    (OUT.parent / "http_audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in summary:
        print(item["source"], item.get("status", "ERROR"), item["requested_url"], item.get("h1", []), item.get("error", ""))
