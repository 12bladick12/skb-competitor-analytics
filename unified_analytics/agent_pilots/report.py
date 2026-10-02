"""Self-contained HTML for local inspection; all model strings are escaped."""
import base64
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from .documents import FIELDS


def esc(value):
    return escape(str(value), quote=True)


def write_report(path, title, summary, findings=(), records=(), tasks=()):
    parts = ['<!doctype html><html lang="ru"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>' + esc(title) + '</title>',
             '''<style>body{font:16px/1.55 system-ui,sans-serif;margin:32px auto;padding:0 24px;
             max-width:1120px;color:#172635;background:#f6f8fa;overflow-wrap:anywhere}h1{font-size:30px}h2{font-size:22px}
             article,details{background:white;border:1px solid #d8e0e7;border-radius:10px;padding:18px;margin:16px 0}
             summary{cursor:pointer;font-weight:650}table{border-collapse:collapse;width:100%;margin:16px 0}
             td,th{border-bottom:1px solid #d8e0e7;text-align:left;padding:10px;vertical-align:top;overflow-wrap:anywhere}
             th{background:#edf2f6}td:nth-child(2){min-width:125px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 monospace}
             .critical,.unknown{border-left:5px solid #b72d2d}.warning{border-left:5px solid #a76900}
             .frame{position:relative;max-width:900px;margin:auto}.frame img{width:100%;display:block}
             .frame svg{position:absolute;inset:0;width:100%;height:100%}.scroll{overflow:auto}
             .muted{color:#52616f}small{font-size:14px}</style>''',
             '<h1>' + esc(title) + '</h1><p class="muted">Отчёт сформирован: '
             + datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
             + '</p><p>' + esc(summary) + '</p>']
    for finding in findings:
        parts.append('<article class="' + esc(finding['severity']) + '"><b>' + esc(finding['message'])
                     + '</b><pre>' + esc(json.dumps(finding['evidence'], ensure_ascii=False, indent=2)) + '</pre></article>')
    for record in records:
        task, reply = record['task'], record['reply']
        result = reply['result']
        origin_label = {'api': 'API', 'session_assisted': 'разбор в текущей сессии', 'manual': 'ручной ввод'}.get(record['origin'], record['origin'])
        parts.append('<details><summary>' + esc(task['requested_variant']) + '</summary><p>Статус: требует проверки. '
                     'Источник результата: ' + esc(origin_label) + '; модель: ' + esc(reply['model']) + '</p>'
                     '<p>' + esc(result['applicability_evidence']) + '</p><div class="scroll"><table><tr>'
                     '<th>Поле</th><th>Значение</th><th>Основание и база измерения</th></tr>')
        for field in result['fields']:
            value = 'Не подтверждено' if field['value'] is None else str(field['value']) + ' ' + field['unit']
            parts.append('<tr><td>' + esc(FIELDS[field['name']]) + '</td><td>' + esc(value) + '</td><td>'
                         + esc(field['evidence']) + '<br><small>' + esc(field['datum']) + '</small></td></tr>')
        parts.append('</table></div>')
        for im in task['images']:
            image = base64.b64encode(Path(im['path']).read_bytes()).decode('ascii')
            parts.append('<p>Страница ' + esc(im['page']) + '. Рамки предложены моделью и требуют проверки.</p>'
                         '<div class="frame"><img alt="Чертёж из паспорта" src="data:image/png;base64,' + image
                         + '"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">')
            for field in result['fields']:
                if field['page'] == im['page'] and field['bbox']:
                    x0, y0, x1, y1 = [v * 100 for v in field['bbox']]
                    parts.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" '
                                 'fill="none" stroke="#b72d2d" stroke-width=".3"><title>'
                                 + esc(FIELDS[field['name']]) + '</title></rect>')
            parts.append('</svg></div>')
        parts.append('<p class="muted">SHA-256 исходного документа: ' + esc(task['source_document_sha256'])
                     + '</p></details>')
    if tasks:
        parts.append('<h2>Подготовленные документы</h2><ul>')
        labels = {'development_internal': 'проверка метода на собственной продукции', 'holdout_competitor': 'независимая выборка конкурентов'}
        parts.extend('<li>' + esc(t['requested_variant']) + ' — ' + esc(labels.get(t['dataset'], t['dataset'])) + '</li>' for t in tasks)
        parts.append('</ul>')
    parts.append('<p class="muted">Пилот не изменяет рабочие цены и подтверждённые характеристики. '
                 'Сведения без проверки не означают взаимозаменяемость изделий.</p></html>')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(parts), encoding='utf-8')
    return path
