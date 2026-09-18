from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from .classifier import RuleBasedClassifier
from .models import AppSettings, CompetitorConfig
from .site_crawler import PublicationDateEvidence, SiteCrawler
from .storage import Storage
from .utils import safe_filename, utcnow_naive


@dataclass
class NewsAuditRecord:
    page_id: int
    competitor: str
    url: str
    content_kind: str
    publication_date_status: str
    published_at: str | None
    published_at_source: str
    reason: str
    rechecked_live: bool


class NewsDateAuditor:
    """Revalidate historical news without discarding the original evidence."""

    def __init__(self, storage: Storage, settings: AppSettings, competitors: list[CompetitorConfig]):
        self.storage = storage
        self.settings = settings
        self.competitors = {item.code: item for item in competitors}
        self.crawler = SiteCrawler(settings)
        self.classifier = RuleBasedClassifier()
        self._listing_cache: dict[str, dict[str, PublicationDateEvidence]] = {}

    def audit(self, apply: bool = False) -> dict[str, object]:
        rows = self.storage.fetchall(
            """
            SELECT p.*, c.code AS competitor_code, c.name AS competitor_name
            FROM pages p JOIN competitors c ON c.id = p.competitor_id
            WHERE p.source_type = 'news'
            ORDER BY c.code, p.id
            """
        )
        backup_path = str(self.storage.create_backup()) if apply and rows else None
        records: list[NewsAuditRecord] = []

        for row in rows:
            competitor = self.competitors.get(row["competitor_code"])
            if competitor is None:
                continue
            content_kind = self._content_kind(row["url"], competitor)
            evidence = PublicationDateEvidence()
            reason = ""
            live_html = ""
            live_title = ""
            live_text = ""
            rechecked_live = False
            snapshot_path = self._resolve_snapshot_path(row["local_snapshot_path"], competitor.code)

            if content_kind != "article":
                reason = "Страница не является отдельной новостной статьёй."
            else:
                if snapshot_path and snapshot_path.exists():
                    evidence = self._evidence_from_html(snapshot_path.read_text(encoding="utf-8", errors="replace"), competitor)
                else:
                    reason = "Сохранённый HTML-снимок отсутствует."

                if evidence.status != "verified":
                    listing_evidence = self._listing_evidence(competitor, row["url"])
                    if listing_evidence.status == "verified":
                        evidence = listing_evidence

                needs_recheck = evidence.status != "verified" or self._looks_mojibake(row["title"] or "") or self._looks_mojibake(row["text"] or "")
                if needs_recheck:
                    live_html, _, fetch_status = self.crawler.fetch(row["url"], crawl_mode=competitor.crawl_mode)
                    rechecked_live = True
                    if live_html:
                        live_title, live_text, live_soup = self.crawler.parse_html(live_html)
                        live_evidence = self.crawler.extract_publication_date(live_soup, competitor.news_profile)
                        if live_evidence.status == "verified" or evidence.status != "verified":
                            evidence = live_evidence
                        if evidence.status != "verified":
                            reason = "На странице найдено несколько дат либо дата публикации не подтверждена."
                    elif not reason:
                        reason = f"Повторная проверка страницы не удалась: {fetch_status}."

                if evidence.status == "verified":
                    reason = (
                        "Дата подтверждена карточкой соответствующей новости на странице списка."
                        if evidence.source == "listing_card"
                        else "Дата подтверждена датирующим элементом самой публикации."
                    )
                elif evidence.status == "ambiguous":
                    reason = "На странице найдено несколько конкурирующих дат публикации."
                elif not reason:
                    reason = "Дата публикации не найдена в разрешённых элементах страницы."

            published_at = evidence.published_at if content_kind == "article" and evidence.status == "verified" else None
            record = NewsAuditRecord(
                page_id=int(row["id"]),
                competitor=row["competitor_name"],
                url=row["url"],
                content_kind=content_kind,
                publication_date_status=evidence.status if content_kind == "article" else "missing",
                published_at=published_at.date().isoformat() if published_at else None,
                published_at_source=evidence.source if published_at else "missing",
                reason=reason,
                rechecked_live=rechecked_live,
            )
            records.append(record)

            if apply:
                new_snapshot_path = str(snapshot_path) if snapshot_path and str(snapshot_path) != (row["local_snapshot_path"] or "") else None
                if live_html:
                    new_snapshot_path = str(self.crawler.save_snapshot(competitor.code, "news", row["url"], live_html))
                    self.storage.refresh_page_content(
                        record.page_id,
                        live_title,
                        live_text,
                        live_html,
                        self.classifier.classify(live_title, live_text, "news"),
                    )
                self.storage.apply_page_date_audit(
                    page_id=record.page_id,
                    published_at=published_at,
                    source=evidence.source if published_at else "missing",
                    evidence=evidence.evidence,
                    status=record.publication_date_status,
                    content_kind=content_kind,
                    reason=reason,
                    checked_at=utcnow_naive(),
                    local_snapshot_path=new_snapshot_path,
                )

        return {
            "total": len(records),
            "verified": sum(item.publication_date_status == "verified" for item in records),
            "ambiguous": sum(item.publication_date_status == "ambiguous" for item in records),
            "missing": sum(item.publication_date_status == "missing" for item in records),
            "non_articles": sum(item.content_kind != "article" for item in records),
            "live_rechecks": sum(item.rechecked_live for item in records),
            "applied": apply,
            "backup_path": backup_path,
            "records": [asdict(item) for item in records],
        }

    def _evidence_from_html(self, html: str, competitor: CompetitorConfig) -> PublicationDateEvidence:
        _, _, soup = self.crawler.parse_html(html)
        return self.crawler.extract_publication_date(soup, competitor.news_profile)

    @staticmethod
    def _url_key(url: str) -> str:
        return url.split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()

    def _listing_evidence(self, competitor: CompetitorConfig, article_url: str) -> PublicationDateEvidence:
        if competitor.code not in self._listing_cache:
            dates: dict[str, PublicationDateEvidence] = {}
            snapshot_root = self.settings.snapshots_dir / competitor.code
            if snapshot_root.exists():
                for snapshot_path in self._listing_snapshot_paths(snapshot_root, competitor):
                    try:
                        html = snapshot_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    _, _, soup = self.crawler.parse_html(html)
                    canonical = soup.select_one("link[rel='canonical'][href]")
                    open_graph = soup.select_one("meta[property='og:url'][content]")
                    base_url = (
                        canonical.get("href") if canonical
                        else open_graph.get("content") if open_graph
                        else competitor.base_url
                    )
                    self._remember_listing_items(
                        dates,
                        self.crawler.extract_listing_items(str(base_url), soup, competitor.news_profile),
                    )
            for section_url in competitor.sections.news:
                html, _, _ = self.crawler.fetch(section_url, crawl_mode=competitor.crawl_mode)
                if not html:
                    continue
                _, _, soup = self.crawler.parse_html(html)
                self._remember_listing_items(
                    dates,
                    self.crawler.extract_listing_items(section_url, soup, competitor.news_profile),
                )
            self._listing_cache[competitor.code] = dates
        dates = self._listing_cache[competitor.code]
        for key in self._listing_keys(article_url):
            evidence = dates.get(key)
            if evidence and evidence.status == "verified":
                return evidence
        return PublicationDateEvidence()

    @staticmethod
    def _listing_snapshot_paths(snapshot_root: Path, competitor: CompetitorConfig) -> list[Path]:
        selected: set[Path] = set()
        for section_url in competitor.sections.news:
            token = safe_filename(section_url).lower()
            selected.update(snapshot_root.rglob(f"*_{token}.html"))
        for path in snapshot_root.rglob("news/*.html"):
            name = path.name.lower()
            if any(marker in name for marker in ("_newscategory_", "_news.html", "_articles.html", "_text-news.html", "_product-news.html")):
                selected.add(path)
        return sorted(selected)

    def _remember_listing_items(self, dates: dict[str, PublicationDateEvidence], items: list[dict[str, object]]) -> None:
        for item in items:
            if item.get("publication_date_status") != "verified" or not item.get("published_at"):
                continue
            evidence = PublicationDateEvidence(
                published_at=item["published_at"],
                source="listing_card",
                evidence=str(item.get("published_at_evidence", "")),
                status="verified",
            )
            for key in self._listing_keys(str(item["url"])):
                previous = dates.get(key)
                if previous and previous.published_at and previous.published_at.date() != evidence.published_at.date():
                    dates[key] = PublicationDateEvidence(
                        source="listing_card",
                        evidence=f"{previous.evidence}; {evidence.evidence}",
                        status="ambiguous",
                    )
                elif not previous:
                    dates[key] = evidence

    def _listing_keys(self, url: str) -> list[str]:
        path = urlparse(url).path.rstrip("/").lower()
        slug = path.rsplit("/", 1)[-1] if path else ""
        return [f"url:{self._url_key(url)}", f"path:{path}", f"slug:{slug}"]

    def _resolve_snapshot_path(self, stored_path: str | None, competitor_code: str) -> Path | None:
        if not stored_path:
            return None
        path = Path(stored_path)
        if path.exists():
            return path
        snapshot_root = self.settings.snapshots_dir / competitor_code
        if not snapshot_root.exists():
            return path
        matches = sorted(snapshot_root.rglob(path.name), key=lambda item: item.stat().st_mtime, reverse=True)
        return matches[0] if matches else path

    def _content_kind(self, url: str, competitor: CompetitorConfig) -> str:
        normalized_url = url.split("?", 1)[0].rstrip("/").lower()
        configured_sections = {item.rstrip("/").lower() for item in competitor.sections.news}
        if normalized_url in configured_sections:
            return "listing"
        path = url.split("?", 1)[0].rstrip("/").lower()
        if path.endswith(("/news", "/articles", "/new-products", "/text-news")):
            return "listing"
        if self.crawler._is_news_article_url("", url, competitor.news_profile):
            return "article"
        return "utility"

    @staticmethod
    def _looks_mojibake(value: str) -> bool:
        if any(marker in value for marker in ("Ð", "Ñ", "Ã", "Â")):
            return True
        return sum("À" <= character <= "ÿ" for character in value) >= 4
