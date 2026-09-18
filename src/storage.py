from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ClassificationResult, CompetitorConfig, CrawlResult, ProductCandidate
from .utils import compute_hash


def dt(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS competitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                base_url TEXT
            );
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER NOT NULL,
                source_type TEXT,
                url TEXT NOT NULL,
                title TEXT,
                discovered_at TEXT,
                last_checked_at TEXT,
                status TEXT,
                UNIQUE(competitor_id, url, source_type),
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                text TEXT,
                html_hash TEXT,
                text_hash TEXT,
                published_at TEXT,
                discovered_at TEXT,
                checked_at TEXT,
                source_type TEXT,
                local_snapshot_path TEXT,
                classification_category TEXT,
                classification_confidence REAL,
                matched_keywords TEXT,
                short_summary TEXT,
                attachments_json TEXT,
                product_mentions_json TEXT,
                UNIQUE(competitor_id, url, text_hash),
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                marking TEXT,
                product_type TEXT,
                url TEXT,
                product_url TEXT,
                site_added_at TEXT,
                site_added_evidence_url TEXT,
                evidence_type TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                characteristics_json TEXT,
                evidence_page_id INTEGER,
                status TEXT,
                UNIQUE(competitor_id, name, url),
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER NOT NULL,
                change_type TEXT,
                entity_type TEXT,
                entity_name TEXT,
                url TEXT,
                old_value TEXT,
                new_value TEXT,
                detected_at TEXT,
                period_month TEXT,
                importance TEXT,
                comment TEXT,
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );
            CREATE TABLE IF NOT EXISTS telegram_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT,
                url TEXT,
                post_id TEXT,
                text TEXT,
                published_at TEXT,
                discovered_at TEXT,
                media_links TEXT,
                text_hash TEXT,
                UNIQUE(channel, post_id, text_hash)
            );
            CREATE TABLE IF NOT EXISTS page_date_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL,
                previous_published_at TEXT,
                validated_published_at TEXT,
                published_at_source TEXT,
                published_at_evidence TEXT,
                publication_date_status TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                reason TEXT,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(page_id) REFERENCES pages(id)
            );
            CREATE TABLE IF NOT EXISTS site_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                include_filters_json TEXT NOT NULL DEFAULT '[]',
                subtractive_selectors_json TEXT NOT NULL DEFAULT '[]',
                content_keywords_json TEXT NOT NULL DEFAULT '[]',
                crawl_mode TEXT NOT NULL DEFAULT 'http',
                baseline_timestamp TEXT,
                last_imported_timestamp TEXT,
                last_snapshot_hash TEXT,
                last_snapshot_text TEXT,
                last_snapshot_html TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(competitor_id, source_type, url),
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );
            CREATE TABLE IF NOT EXISTS site_change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_watch_id INTEGER NOT NULL,
                history_timestamp TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                published_at TEXT,
                publication_date_status TEXT NOT NULL DEFAULT 'missing',
                publication_date_source TEXT,
                publication_date_evidence TEXT,
                event_kind TEXT NOT NULL,
                article_url TEXT,
                title TEXT,
                summary TEXT,
                diff_text TEXT,
                before_snapshot TEXT,
                after_snapshot TEXT,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'observed',
                UNIQUE(site_watch_id, history_timestamp),
                FOREIGN KEY(site_watch_id) REFERENCES site_watches(id)
            );
            """
        )
        self._ensure_column("products", "marking", "TEXT")
        self._ensure_column("products", "product_url", "TEXT")
        self._ensure_column("products", "site_added_at", "TEXT")
        self._ensure_column("products", "site_added_evidence_url", "TEXT")
        self._ensure_column("products", "evidence_type", "TEXT")
        self._ensure_column("pages", "published_at_source", "TEXT")
        self._ensure_column("pages", "published_at_evidence", "TEXT")
        self._ensure_column("pages", "publication_date_status", "TEXT NOT NULL DEFAULT 'missing'")
        self._ensure_column("pages", "content_kind", "TEXT NOT NULL DEFAULT 'article'")
        self._ensure_column("site_watches", "last_snapshot_hash", "TEXT")
        self._ensure_column("site_watches", "last_snapshot_text", "TEXT")
        self._ensure_column("site_watches", "last_snapshot_html", "TEXT")
        self._ensure_column("site_watches", "crawl_mode", "TEXT NOT NULL DEFAULT 'http'")
        self._ensure_column("site_watches", "content_keywords_json", "TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_competitor(self, competitor: CompetitorConfig) -> int:
        self.conn.execute(
            """
            INSERT INTO competitors(name, code, base_url)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET name=excluded.name, base_url=excluded.base_url
            """,
            (competitor.name, competitor.code, competitor.base_url),
        )
        self.conn.commit()
        return self.get_competitor_id(competitor.code)

    def get_competitor_id(self, code: str) -> int:
        row = self.conn.execute("SELECT id FROM competitors WHERE code = ?", (code,)).fetchone()
        if not row:
            raise KeyError(f"Competitor not found: {code}")
        return int(row["id"])

    def upsert_source(self, competitor_id: int, source_type: str, url: str, title: str, status: str, checked_at: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO sources(competitor_id, source_type, url, title, discovered_at, last_checked_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(competitor_id, url, source_type) DO UPDATE SET
                title=excluded.title,
                last_checked_at=excluded.last_checked_at,
                status=excluded.status
            """,
            (competitor_id, source_type, url, title, dt(checked_at), dt(checked_at), status),
        )
        self.conn.commit()

    def insert_page(self, result: CrawlResult, classification: ClassificationResult | None = None) -> int:
        competitor_id = self.get_competitor_id(result.competitor_code)
        if result.source_type == "news" and result.publication_date_status != "verified":
            verified = self.conn.execute(
                """
                SELECT id FROM pages
                WHERE competitor_id = ? AND source_type = 'news' AND url = ?
                  AND publication_date_status = 'verified' AND content_kind = 'article'
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
                """,
                (competitor_id, result.url),
            ).fetchone()
            if verified:
                self.conn.execute(
                    "UPDATE pages SET checked_at = ? WHERE id = ?",
                    (dt(result.checked_at), verified["id"]),
                )
                self.conn.commit()
                return int(verified["id"])
        html_hash = compute_hash(result.html)
        text_hash = compute_hash(result.text)
        classification = classification or ClassificationResult(category="прочее", confidence=0.0)
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO pages(
                competitor_id, url, title, text, html_hash, text_hash, published_at, discovered_at, checked_at,
                source_type, local_snapshot_path, classification_category, classification_confidence,
                matched_keywords, short_summary, attachments_json, product_mentions_json,
                published_at_source, published_at_evidence, publication_date_status, content_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                competitor_id,
                result.url,
                result.title,
                result.text,
                html_hash,
                text_hash,
                dt(result.published_at),
                dt(result.discovered_at),
                dt(result.checked_at),
                result.source_type,
                result.local_snapshot_path,
                classification.category,
                classification.confidence,
                json.dumps(classification.matched_keywords, ensure_ascii=False),
                classification.short_summary,
                json.dumps(result.attachment_links, ensure_ascii=False),
                json.dumps(result.product_mentions, ensure_ascii=False),
                result.published_at_source,
                result.published_at_evidence,
                result.publication_date_status,
                result.content_kind,
            ),
        )
        self.conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = self.conn.execute(
            "SELECT id FROM pages WHERE competitor_id = ? AND url = ? AND text_hash = ?",
            (competitor_id, result.url, text_hash),
        ).fetchone()
        if not row:
            return 0
        page_id = int(row["id"])
        self.conn.execute(
            """
            UPDATE pages
            SET title = COALESCE(NULLIF(?, ''), title), checked_at = ?,
                local_snapshot_path = COALESCE(NULLIF(?, ''), local_snapshot_path)
            WHERE id = ?
            """,
            (result.title, dt(result.checked_at), result.local_snapshot_path, page_id),
        )
        if result.publication_date_status == "verified":
            self.conn.execute(
                """
                UPDATE pages
                SET published_at = ?, published_at_source = ?, published_at_evidence = ?,
                    publication_date_status = ?, content_kind = ?
                WHERE id = ?
                """,
                (
                    dt(result.published_at),
                    result.published_at_source,
                    result.published_at_evidence,
                    result.publication_date_status,
                    result.content_kind,
                    page_id,
                ),
            )
        self.conn.commit()
        return page_id

    def create_backup(self) -> Path:
        backup_path = self.db_path.with_name(f"{self.db_path.stem}_{datetime.now():%Y%m%d_%H%M%S}.bak{self.db_path.suffix}")
        destination = sqlite3.connect(backup_path)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
        return backup_path

    def apply_page_date_audit(
        self,
        page_id: int,
        published_at: datetime | None,
        source: str,
        evidence: str,
        status: str,
        content_kind: str,
        reason: str,
        checked_at: datetime,
        local_snapshot_path: str | None = None,
    ) -> None:
        old = self.conn.execute("SELECT published_at FROM pages WHERE id = ?", (page_id,)).fetchone()
        if old is None:
            raise KeyError(f"Page not found: {page_id}")
        self.conn.execute(
            """
            INSERT INTO page_date_audits(
                page_id, previous_published_at, validated_published_at, published_at_source,
                published_at_evidence, publication_date_status, content_kind, reason, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (page_id, old["published_at"], dt(published_at), source, evidence, status, content_kind, reason, dt(checked_at)),
        )
        self.conn.execute(
            """
            UPDATE pages
            SET published_at = ?, published_at_source = ?, published_at_evidence = ?,
                publication_date_status = ?, content_kind = ?, checked_at = ?,
                local_snapshot_path = COALESCE(?, local_snapshot_path)
            WHERE id = ?
            """,
            (dt(published_at), source, evidence, status, content_kind, dt(checked_at), local_snapshot_path, page_id),
        )
        self.conn.commit()

    def refresh_page_content(self, page_id: int, title: str, text: str, html: str, classification: ClassificationResult) -> None:
        self.conn.execute(
            """
            UPDATE pages
            SET title = ?, text = ?, html_hash = ?, text_hash = ?,
                classification_category = ?, classification_confidence = ?,
                matched_keywords = ?, short_summary = ?
            WHERE id = ?
            """,
            (
                title,
                text,
                compute_hash(html),
                compute_hash(text),
                classification.category,
                classification.confidence,
                json.dumps(classification.matched_keywords, ensure_ascii=False),
                classification.short_summary,
                page_id,
            ),
        )
        self.conn.commit()

    def upsert_product(self, product: ProductCandidate) -> tuple[int, bool, str | None]:
        competitor_id = self.get_competitor_id(product.competitor_code)
        old = self.conn.execute(
            "SELECT * FROM products WHERE competitor_id = ? AND name = ? AND url = ?",
            (competitor_id, product.name, product.url),
        ).fetchone()
        new_characteristics = json.dumps(product.characteristics, ensure_ascii=False, sort_keys=True)
        if old is None:
            cur = self.conn.execute(
                """
                INSERT INTO products(competitor_id, name, marking, product_type, url, product_url,
                                     site_added_at, site_added_evidence_url, evidence_type, first_seen_at, last_seen_at,
                                     characteristics_json, evidence_page_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    competitor_id,
                    product.name,
                    product.marking,
                    product.product_type,
                    product.url,
                    product.product_url or product.url,
                    dt(product.site_added_at),
                    product.site_added_evidence_url,
                    product.evidence_type,
                    dt(product.first_seen_at),
                    dt(product.last_seen_at),
                    new_characteristics,
                    product.evidence_page_id,
                    product.status,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid), True, None

        old_characteristics = old["characteristics_json"] or "{}"
        self.conn.execute(
            """
            UPDATE products
            SET marking = ?, product_type = ?, product_url = ?, site_added_at = COALESCE(site_added_at, ?),
                site_added_evidence_url = COALESCE(NULLIF(site_added_evidence_url, ''), ?),
                evidence_type = CASE
                    WHEN COALESCE(evidence_type, '') IN ('', 'unknown_date') THEN ?
                    ELSE evidence_type
                END,
                last_seen_at = ?, characteristics_json = ?, evidence_page_id = ?, status = ?
            WHERE id = ?
            """,
            (
                product.marking,
                product.product_type,
                product.product_url or product.url,
                dt(product.site_added_at),
                product.site_added_evidence_url,
                product.evidence_type,
                dt(product.last_seen_at),
                new_characteristics,
                product.evidence_page_id,
                product.status,
                old["id"],
            ),
        )
        self.conn.commit()
        changed = old_characteristics != new_characteristics
        return int(old["id"]), False, old_characteristics if changed else None

    def insert_change(
        self,
        competitor_code: str,
        change_type: str,
        entity_type: str,
        entity_name: str,
        url: str,
        old_value: str | None,
        new_value: str | None,
        detected_at: datetime,
        period_month: str,
        importance: str,
        comment: str,
    ) -> None:
        competitor_id = self.get_competitor_id(competitor_code)
        self.conn.execute(
            """
            INSERT INTO changes(competitor_id, change_type, entity_type, entity_name, url, old_value,
                                new_value, detected_at, period_month, importance, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (competitor_id, change_type, entity_type, entity_name, url, old_value, new_value, dt(detected_at), period_month, importance, comment),
        )
        self.conn.commit()

    def insert_telegram_post(self, channel: str, url: str, post_id: str, text: str, published_at: datetime | None, discovered_at: datetime, media_links: list[str]) -> None:
        self.conn.execute(
            """
            INSERT INTO telegram_posts(channel, url, post_id, text, published_at, discovered_at, media_links, text_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, post_id, text_hash) DO UPDATE SET
                url = excluded.url,
                published_at = COALESCE(excluded.published_at, telegram_posts.published_at),
                discovered_at = excluded.discovered_at,
                media_links = excluded.media_links
            """,
            (channel, url, post_id, text, dt(published_at), dt(discovered_at), json.dumps(media_links, ensure_ascii=False), compute_hash(text)),
        )
        self.conn.commit()

    def upsert_site_watch(
        self,
        competitor_code: str,
        source_type: str,
        url: str,
        title: str,
        include_filters: list[str],
        subtractive_selectors: list[str],
        crawl_mode: str,
        content_keywords: list[str] | None = None,
        status: str = "configured",
        last_error: str = "",
    ) -> dict[str, Any]:
        """Create or refresh a curated local watch without duplicating it."""
        competitor_id = self.get_competitor_id(competitor_code)
        now = dt(datetime.now())
        self.conn.execute(
            """
            INSERT INTO site_watches(
                competitor_id, source_type, url, title,
                include_filters_json, subtractive_selectors_json, content_keywords_json, crawl_mode,
                status, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(competitor_id, source_type, url) DO UPDATE SET
                title=excluded.title,
                include_filters_json=excluded.include_filters_json,
                subtractive_selectors_json=excluded.subtractive_selectors_json,
                content_keywords_json=excluded.content_keywords_json,
                crawl_mode=excluded.crawl_mode,
                status=excluded.status,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                competitor_id,
                source_type,
                url,
                title,
                json.dumps(include_filters, ensure_ascii=False),
                json.dumps(subtractive_selectors, ensure_ascii=False),
                json.dumps(content_keywords or [], ensure_ascii=False),
                crawl_mode,
                status,
                last_error,
                now,
                now,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            """
            SELECT sw.*, c.code AS competitor_code, c.name AS competitor_name, c.base_url AS competitor_base_url
            FROM site_watches sw JOIN competitors c ON c.id = sw.competitor_id
            WHERE sw.competitor_id = ? AND sw.source_type = ? AND sw.url = ?
            """,
            (competitor_id, source_type, url),
        ).fetchone()
        return dict(row)

    def list_site_watches(self, competitor_codes: list[str] | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT sw.*, c.code AS competitor_code, c.name AS competitor_name, c.base_url AS competitor_base_url
            FROM site_watches sw JOIN competitors c ON c.id = sw.competitor_id
        """
        params: tuple[Any, ...] = ()
        if competitor_codes:
            placeholders = ",".join("?" for _ in competitor_codes)
            query += f" WHERE c.code IN ({placeholders})"
            params = tuple(competitor_codes)
        query += " ORDER BY c.name, sw.source_type, sw.url"
        return [dict(row) for row in self.conn.execute(query, params).fetchall()]

    def update_site_watch_state(
        self,
        watch_id: int,
        *,
        baseline_timestamp: str | None = None,
        last_imported_timestamp: str | None = None,
        last_snapshot_hash: str | None = None,
        last_snapshot_text: str | None = None,
        last_snapshot_html: str | None = None,
        status: str | None = None,
        last_error: str | None = None,
    ) -> None:
        updates = ["updated_at = ?"]
        values: list[Any] = [dt(datetime.now())]
        for column, value in (
            ("baseline_timestamp", baseline_timestamp),
            ("last_imported_timestamp", last_imported_timestamp),
            ("last_snapshot_hash", last_snapshot_hash),
            ("last_snapshot_text", last_snapshot_text),
            ("last_snapshot_html", last_snapshot_html),
            ("status", status),
            ("last_error", last_error),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(value)
        values.append(watch_id)
        self.conn.execute(f"UPDATE site_watches SET {', '.join(updates)} WHERE id = ?", tuple(values))
        self.conn.commit()

    def insert_site_change_event(
        self,
        *,
        site_watch_id: int,
        history_timestamp: str,
        detected_at: datetime,
        published_at: datetime | None,
        publication_date_status: str,
        publication_date_source: str,
        publication_date_evidence: str,
        event_kind: str,
        article_url: str,
        title: str,
        summary: str,
        diff_text: str,
        before_snapshot: str,
        after_snapshot: str,
        source_url: str,
        status: str,
    ) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO site_change_events(
                site_watch_id, history_timestamp, detected_at, published_at,
                publication_date_status, publication_date_source, publication_date_evidence,
                event_kind, article_url, title, summary, diff_text, before_snapshot,
                after_snapshot, source_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site_watch_id,
                history_timestamp,
                dt(detected_at),
                dt(published_at),
                publication_date_status,
                publication_date_source,
                publication_date_evidence,
                event_kind,
                article_url,
                title,
                summary,
                diff_text,
                before_snapshot,
                after_snapshot,
                source_url,
                status,
            ),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def site_activity_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.*, c.name AS competitor_name, c.code AS competitor_code
            FROM site_change_events e
            JOIN site_watches sw ON sw.id = e.site_watch_id
            JOIN competitors c ON c.id = sw.competitor_id
            WHERE e.detected_at BETWEEN ? AND ?
            ORDER BY COALESCE(e.published_at, e.detected_at) DESC, e.id DESC
            """,
            (dt(start), dt(end)),
        ).fetchall()
        return [dict(row) for row in rows]

    def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(query, params).fetchall())

    def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(query, params).fetchone()
