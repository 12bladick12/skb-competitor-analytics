from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from dateutil.relativedelta import relativedelta

from .logger import logger
from .models import ReportPeriod


def ensure_directories(paths: list[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(lambda msg: print(msg, end="", flush=True), level="INFO", backtrace=False, diagnose=False)
    logger.add(logs_dir / "competitor_monitor.log", rotation="5 MB", retention=10, encoding="utf-8", level="DEBUG", backtrace=False, diagnose=False)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def compute_hash(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="ignore")
    return hashlib.sha256(value).hexdigest()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_filename(value: str, max_len: int = 120) -> str:
    value = re.sub(r"[^\w\-.а-яА-Я]+", "_", value, flags=re.IGNORECASE).strip("_")
    return (value or "item")[:max_len]


def build_report_period(year: int, month: int | None = None, now: datetime | None = None) -> ReportPeriod:
    now = now or datetime.now()
    if month is None:
        start = datetime(year, 1, 1)
        year_end = datetime(year, 12, 31, 23, 59, 59)
        end = now if year == now.year else year_end
        end = end.replace(microsecond=0)
        label = f"{start:%d.%m.%Y}-{end:%d.%m.%Y}"
        return ReportPeriod(year=year, month=None, start=start, end=end, label=label, granularity="year")

    start = datetime(year, month, 1)
    next_month = start + relativedelta(months=1)
    month_end = next_month - relativedelta(days=1)
    end = now if year == now.year and month == now.month else month_end
    end = end.replace(hour=23, minute=59, second=59, microsecond=0) if end.date() == month_end.date() else end
    end = end.replace(microsecond=0)
    label = f"{start:%d.%m.%Y}-{end:%d.%m.%Y}"
    return ReportPeriod(year=year, month=month, start=start, end=end, label=label, granularity="month")


def parse_report_period(value: str, now: datetime | None = None) -> ReportPeriod:
    value = normalize_text(value)
    interval = re.fullmatch(r'(\d{4}-\d{2}-\d{2})__(\d{4}-\d{2}-\d{2})', value)
    if interval:
        return build_date_range(interval[1], interval[2])
    match = re.fullmatch(r"(\d{4})(?:[-./](\d{1,2}))?", value)
    if not match:
        raise ValueError("Use YYYY for a full year or YYYY-MM for a month, for example 2026 or 2026-06.")

    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    if month is not None and not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    return build_report_period(year, month, now=now)


def report_period_key(period: ReportPeriod) -> str:
    if period.granularity == 'range':
        return f'{period.start:%Y-%m-%d}__{period.end:%Y-%m-%d}'
    if period.month is None:
        return f"{period.year:04d}"
    return f"{period.year:04d}-{period.month:02d}"


def report_period_keys(period: ReportPeriod) -> list[str]:
    if period.granularity == 'range':
        cursor = period.start.replace(day=1)
        keys = [report_period_key(period)]
        while cursor <= period.end:
            keys.append(f'{cursor:%Y-%m}')
            cursor += relativedelta(months=1)
        return keys
    if period.month is None:
        return [f"{period.year:04d}"] + [f"{period.year:04d}-{month:02d}" for month in range(1, 13)]
    return [report_period_key(period)]


def build_date_range(date_from: str, date_to: str) -> ReportPeriod:
    if not all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) for value in (date_from,date_to)):
        raise ValueError('Даты должны быть в формате ГГГГ-ММ-ДД')
    try:
        start=datetime.fromisoformat(date_from)
        end=datetime.fromisoformat(date_to).replace(hour=23,minute=59,second=59)
    except ValueError as exc:
        raise ValueError('Указана несуществующая дата') from exc
    if start > end:
        raise ValueError('Дата начала не может быть позже даты окончания')
    return ReportPeriod(year=start.year,month=None,start=start,end=end,
        label=f'от {start:%d.%m.%Y} по {end:%d.%m.%Y}',granularity='range')


def parse_datetime_or_none(value: str | None) -> datetime | None:
    """Parse only explicit publisher date formats.

    This function deliberately does not use fuzzy parsing: callers must first
    locate a publication-date element, otherwise arbitrary numbers in page text
    can be mistaken for a publication date.
    """
    if not value:
        return None
    value = normalize_text(value)
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})(?:[T\s]|$)", value)
    if iso_match:
        try:
            year, month, day = (int(part) for part in iso_match.group(1).split("-"))
            return datetime(year, month, day)
        except ValueError:
            return None

    year_first_numeric_match = re.search(r"\b(\d{4})[./](\d{1,2})[./](\d{1,2})\b", value)
    if year_first_numeric_match:
        try:
            return datetime(
                int(year_first_numeric_match.group(1)),
                int(year_first_numeric_match.group(2)),
                int(year_first_numeric_match.group(3)),
            )
        except ValueError:
            return None

    numeric_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", value)
    if numeric_match:
        try:
            return datetime(int(numeric_match.group(3)), int(numeric_match.group(2)), int(numeric_match.group(1)))
        except ValueError:
            return None
    russian_months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
        "янв": 1,
        "фев": 2,
        "мар": 3,
        "апр": 4,
        "май": 5,
        "июн": 6,
        "июл": 7,
        "авг": 8,
        "сен": 9,
        "сент": 9,
        "окт": 10,
        "ноя": 11,
        "дек": 12,
    }
    match = re.search(
        r"\b(\d{1,2})\s+("
        + "|".join(sorted(russian_months, key=len, reverse=True))
        + r")\s+(\d{4})(?:\s*(?:г\.|года))?\b",
        value.lower(),
    )
    if match:
        day = int(match.group(1))
        month = russian_months[match.group(2)]
        year = int(match.group(3))
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    english_formats = (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B-%d-%Y",
        "%b-%d-%Y",
    )
    english_match = re.search(
        r"\b(?:[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}-\d{1,2}-\d{4})\b",
        value,
    )
    if english_match:
        candidate = english_match.group(0)
        for fmt in english_formats:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def join_url(base: str, href: str) -> str:
    return urljoin(base, href)


ALLOWED_URL_SCHEMES = {"http", "https"}
IGNORED_FILE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".mp4",
    ".mp3",
    ".avi",
    ".mov",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
}


def normalize_crawl_url(base: str, href: str | None, allowed_file_extensions: tuple[str, ...] = ()) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href == "#" or href.startswith("#"):
        return None
    if re.match(r"^(javascript|mailto|tel):", href, flags=re.IGNORECASE):
        return None

    url = urljoin(base, href)
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return None

    path_lower = parsed.path.lower()
    suffix = Path(path_lower).suffix
    if suffix in IGNORED_FILE_EXTENSIONS:
        return None
    if suffix and suffix not in allowed_file_extensions and suffix not in {".html", ".htm", ".php", ".aspx"}:
        return None

    # Preserve the publisher's canonical path. Some CMS routes (for example,
    # Autonics newsroom) return 404 when a trailing slash is appended.
    normalized_path = parsed.path or "/"

    ignored_query_prefixes = ("utm_", "yclid", "gclid", "fbclid")
    query_parts = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key.startswith(ignored_query_prefixes):
            continue
        query_parts.append(part)

    return urlunparse((parsed.scheme, parsed.netloc.lower(), normalized_path, "", "&".join(query_parts), ""))


def is_http_url(url: str | None) -> bool:
    if not url:
        return False
    return urlparse(url).scheme.lower() in ALLOWED_URL_SCHEMES


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(str(self.path), flags)
            payload = f"pid={os.getpid()}\nstarted_at={datetime.now().isoformat(timespec='seconds')}\n"
            os.write(self.fd, payload.encode("utf-8"))
        except FileExistsError as exc:
            recovery = self.path.with_suffix('.recovery')
            recovery_fd = None
            try:
                # Serialize stale-lock recovery so two starters cannot delete
                # the fresh lock created by the first recovering process.
                recovery_fd = os.open(str(recovery), flags)
                existing = self.path.read_text(encoding="utf-8")
                match = re.search(r'^pid=(\d+)$',existing,re.M)
                if match and not self._process_alive(int(match[1])):
                    self.path.unlink(missing_ok=True)
                    return self.__enter__()
            except (OSError, ValueError):
                existing = 'Lock recovery is already active or lock cannot be read.'
            finally:
                if recovery_fd is not None:
                    os.close(recovery_fd)
                    recovery.unlink(missing_ok=True)
            raise RuntimeError(f"Another competitor_monitor run is already active. Lock: {self.path}. {existing}") from exc
        return self

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if os.name=='nt':
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.OpenProcess(0x1000,False,pid)
            if handle:
                kernel.CloseHandle(handle)
                return True
            return ctypes.get_last_error() not in (87,1168)
        try:
            os.kill(pid,0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Cannot remove lock file: {}", self.path)
