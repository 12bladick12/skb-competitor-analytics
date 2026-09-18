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
    st.html('<style>' + Path(__file__).with_name('theme.css').read_text(encoding='utf-8') + '</style>')


def brand():
    logo = Path(__file__).with_name('logo.base64').read_text(encoding='ascii').strip()
    html(f'<div class="brand"><img src="data:image/png;base64,{logo}" alt="СКБ Индукция">'
         '<span class="brand-caption">Конкурентная аналитика</span></div>')


def masthead():
    html('''<div class="masthead"><span class="eyebrow">СКБ ИНДУКЦИЯ / АНАЛИТИЧЕСКИЙ ЦЕНТР</span>
    <span class="workspace-tag">Рабочее пространство</span></div>''')


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
