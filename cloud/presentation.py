"""Presentation only. External text is escaped before insertion into our HTML."""

from collections import Counter
from html import escape
from pathlib import Path

import streamlit as st
from .coverage import monitored, outside_scope


def safe(value):
    return escape(str(value if value is not None else ''), quote=True)


def html(content):
    st.html(content)


def apply_theme():
    css = '\n'.join(Path(__file__).with_name(name).read_text(encoding='utf-8')
                    for name in ('theme.css', 'workspace.css', 'shell.css'))
    st.html('<style>' + css + '</style>')
    # Only this checked-in script is executable; external text stays escaped.
    script = Path(__file__).with_name('shell.js').read_text(encoding='utf-8')
    with st.container(key='skb_shell_setup'):
        st.iframe('<!doctype html><html data-skb-shell-helper><body><script>' + script + '</script></body></html>',
                  height=1, tab_index=-1)


def brand():
    logo = Path(__file__).with_name('logo.base64').read_text(encoding='ascii').strip()
    html(f'<div class="brand"><img src="data:image/png;base64,{logo}" alt="СКБ Индукция">'
         '<span class="brand-caption">Конкурентная аналитика</span></div>')


def masthead():
    html('''<div class="masthead"><span class="eyebrow">СКБ ИНДУКЦИЯ <span class="masthead-divider">/</span> Аналитический центр</span>
    <span class="workspace-tag">Конкурентная аналитика</span></div>''')


def account(email, role):
    html(f'<div class="account"><span class="avatar" aria-hidden="true">{safe(email[:1].upper())}</span>'
         f'<div><div class="account-email">{safe(email)}</div><div class="account-role">{safe(role)}</div></div></div>')


def note(title, text):
    html(f'<div class="context-note"><span class="note-mark" aria-hidden="true">i</span>'
         f'<div><strong>{safe(title)}</strong><span>{safe(text)}</span></div></div>')


def section_heading(title, subtitle=''):
    html(f'<div class="section-heading"><h3>{safe(title)}</h3><span>{safe(subtitle)}</span></div>')


def event_card(event, kind_label, *, compact=False):
    classes = 'publication compact' if compact else 'publication'
    html(f'<article class="{classes}"><div class="publication-meta">'
         f'<span class="company-label">{safe(event["competitor_name"])}</span>'
         f'<span>{safe(kind_label)}</span><span>{safe(event["date_label"])}</span></div>'
         f'<h3>{safe(event["title"])}</h3>'
         f'<p>{safe(event["description"])}</p>'
         '<span class="verified"><span aria-hidden="true">✓</span> Подтверждённый материал</span></article>')


def research_card(item, source_url='', case_url=''):
    """Present saved research without promoting a candidate to a confirmed event."""
    precision = item.get('published_precision')
    published = item.get('published_at')
    date_label = str(published)[:10] if precision == 'day' and published else (
        str(published)[:7] if precision == 'month' and published else 'Дата не установлена')
    status = ('Цитата подтверждена снимком' if item.get('status') == 'verified'
              else 'Требует проверки')
    status_class = 'research-verified' if item.get('status') == 'verified' else 'research-candidate'
    title = item.get('title') or 'Без названия'
    summary = item.get('summary') or ''
    source = item.get('source_name') or 'Источник не указан'
    company = item.get('competitor_name') or item.get('competitor_code') or 'Конкурент не указан'
    case_number = item.get('case_number')
    source_link = (f'<a href="{safe(source_url)}" target="_blank" rel="noopener noreferrer">'
                   'Первоисточник ↗</a>') if source_url else '<span>Ссылка на первоисточник не сохранена</span>'
    case_link = (f'<a href="{safe(case_url)}" target="_blank" rel="noopener noreferrer">'
                 f'{safe(case_number or "Карточка дела")} ↗</a>') if case_url else (
                     f'<span>Дело {safe(case_number)}</span>' if case_number else '')
    evidence = item.get('evidence') or {}
    quote = evidence.get('quote') if isinstance(evidence, dict) else None
    quote_html = f'<blockquote>{safe(quote)}</blockquote>' if quote else ''
    html(f'<article class="research-item"><div class="research-meta">'
         f'<span class="company-label">{safe(company)}</span><span>{safe(date_label)}</span></div>'
         f'<h4>{safe(title)}</h4><p>{safe(summary)}</p>{quote_html}'
         f'<div class="research-source"><span>{safe(source)}</span>{source_link}{case_link}</div>'
         f'<span class="research-status {status_class}">{safe(status)}</span></article>')


def coverage_summary(checks):
    excluded = outside_scope(checks)
    checks = monitored(checks)
    counts = Counter(c.get('status') for c in checks)
    segments = [
        ('success', 'Проверены', '#238777'),
        ('partial', 'Сбор неполный', '#e48a36'),
        ('error', 'Ошибки', '#cb5260'),
    ]
    known = {s[0] for s in segments}
    other = sum(v for k, v in counts.items() if k not in known)
    if other:
        counts['other'] = other
        segments.append(('other', 'Другие состояния', '#67778c'))
    total = len(checks)
    stops, start = [], 0.0
    for key, _, color in segments:
        end = start + (counts[key] * 100 / total if total else 0)
        if end > start:
            stops.append(f'{color} {start:.4f}% {end:.4f}%')
        start = end
    gradient = 'conic-gradient(' + ','.join(stops) + ')' if stops else '#e8edf1'
    rows = ''.join(f'<div class="legend-row"><span><i style="background:{color}" aria-hidden="true"></i>'
                   f'{safe(label)}</span><b>{counts[key]}</b></div>' for key, label, color in segments)
    no_news = sum(c.get('status') == 'success' and c.get('items') == 0 for c in checks)
    html(f'<div class="coverage-visual"><div class="donut" style="background:{gradient}" role="img" '
         f'aria-label="Проверено источников: {counts["success"]} из {total}">'
         f'<div><b>{counts["success"]}<small>из {total}</small></b><span>проверены</span></div></div>'
         f'<div class="coverage-legend">{rows}</div></div>'
         f'<p class="coverage-foot">Из проверенных: {no_news} без публикаций. '
         'Неполный сбор и ошибки учитываются отдельно.</p>')
    if excluded:
        html(f'<p class="coverage-foot">Telegram отслеживается у ТЕКО. Компании без настроенного канала ({excluded}) не входят в число источников.</p>')


def competitor_bars(events, competitors):
    counts = Counter(e['competitor_code'] for e in events)
    maximum = max(counts.values(), default=1)
    rows = []
    for code, number in counts.most_common(5):
        name = competitors.get(code, {}).get('name', code)
        rows.append(f'<div class="company-bar"><div><span>{safe(name)}</span><b>{number}</b></div>'
                    f'<div class="bar-track"><span style="width:{number / maximum * 100:.3f}%"></span></div></div>')
    html('<div class="company-bars">' + ''.join(rows) + '</div>' if rows else '<p class="empty-copy">Нет подтверждённых материалов за период.</p>')
