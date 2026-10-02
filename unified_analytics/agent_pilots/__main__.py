import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from .common import DEFAULT_OUTPUT, ROOT, ModelError, ResponsesClient, read_json, write_json
from .documents import evaluate, extract, prepare, save_proposal, verify_task
from .health import compare_samples, diagnose, inspect_snapshot, live_snapshot, parse_sample, probe_card
from .report import write_report


def parser():
    p = argparse.ArgumentParser(description='Два изолированных пилота: диагностика и паспорта')
    sub = p.add_subparsers(dest='command', required=True)
    health = sub.add_parser('health', help='Диагностика без перезапуска и записи в рабочую БД')
    inputs = health.add_mutually_exclusive_group()
    inputs.add_argument('--snapshot', type=Path)
    inputs.add_argument('--live', action='store_true')
    health.add_argument('--source', default='sensoren', choices=['sensoren', 'beskonta', 'sensor', 'teko', 'megak'])
    health.add_argument('--secrets', type=Path, default=ROOT / '.streamlit' / 'secrets.toml')
    health.add_argument('--replay', action='store_true', help='Проверить архив на момент снимка, не текущее состояние')
    health.add_argument('--baseline', type=Path)
    health.add_argument('--current', type=Path)
    health.add_argument('--rule', type=Path)
    health.add_argument('--model', help='Явно включить один вызов API при наличии предупреждения')
    health.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    probe = sub.add_parser('probe', help='Один контрольный запрос карточки с проверкой robots.txt')
    probe.add_argument('--rule', type=Path, required=True)
    probe.add_argument('--baseline', type=Path, help='Проверенный прежний sample.json из probe')
    probe.add_argument('--model', help='Один вызов диагностики при отклонении')
    probe.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    prep = sub.add_parser('prepare', help='Подготовить 11 паспортов без API')
    prep.add_argument('--source', type=Path, default=ROOT / 'analysis' / 'capacitive_research_2026_09_30' / 'provided_passports')
    prep.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    prep.add_argument('--source-kind', choices=['internal_passport', 'competitor_passport'], default='internal_passport')
    prep.add_argument('--dataset', choices=['development_internal', 'holdout_competitor'], default='development_internal')
    run = sub.add_parser('extract', help='Чтение документов через API, результаты только в локальной базе предложений')
    run.add_argument('--manifest', type=Path, default=DEFAULT_OUTPUT / 'manifest.json')
    run.add_argument('--model', required=True)
    run.add_argument('--limit', type=int, default=1)
    run.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    imp = sub.add_parser('import-result', help='Импорт ответа из текущей сессии; не считается автономным API-прогоном')
    imp.add_argument('--task', type=Path, required=True)
    imp.add_argument('--result', type=Path, required=True)
    imp.add_argument('--origin', choices=['session_assisted', 'manual'], required=True)
    imp.add_argument('--model-label', required=True)
    imp.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    ev = sub.add_parser('evaluate', help='Сверить предложения с отдельной предварительной разметкой')
    ev.add_argument('--gold', type=Path, required=True)
    ev.add_argument('--origin', choices=['api', 'session_assisted', 'manual'], default='api')
    ev.add_argument('--dataset', choices=['development_internal', 'holdout_competitor'], default='development_internal')
    ev.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S', time.gmtime()) + '_' + uuid.uuid4().hex[:8]
    if args.command == 'prepare':
        paths = prepare(args.source, output, args.source_kind, args.dataset)
        write_report(output / 'documents.html', 'Пилот чтения паспортов',
                     f'Подготовлено документов: {len(paths)}. Извлечение моделью ещё не запускалось. ' +
                     ('Имеющиеся паспорта относятся к собственной продукции; внешняя контрольная выборка ещё не подготовлена.'
                      if args.source_kind == 'internal_passport' else 'Паспорта конкурентов; происхождение указано в заданиях.'),
                     tasks=[read_json(path) for path in paths])
        print(f'Подготовлено: {len(paths)}. Отчёт: {output / "documents.html"}')
        return 0
    if args.command == 'health':
        if args.replay and not args.snapshot:
            p.error('--replay требует --snapshot')
        if any((args.baseline, args.current, args.rule)) and not all((args.baseline, args.current, args.rule)):
            p.error('Для сравнения нужны --baseline, --current и --rule')
        if not (args.snapshot or args.live or args.baseline):
            p.error('Укажите --snapshot, --live или пару HTML с --rule')
        findings, samples, snapshot = [], {}, None
        if args.live:
            try:
                snapshot = live_snapshot(args.secrets)
            except Exception as exc:
                findings.append(dict(code='telemetry_unavailable', severity='unknown',
                    message='Живое состояние не прочитано; это не означает простой сборщика',
                    evidence={'error_type': type(exc).__name__}))
        elif args.snapshot:
            snapshot = read_json(args.snapshot)
        if snapshot is not None:
            findings += inspect_snapshot(snapshot, args.source,
                                          snapshot.get('observed_at_epoch') if args.replay else None)
            if args.live:
                write_json(output / 'snapshots' / (stamp + '.json'), snapshot)
        if args.baseline:
            rule = read_json(args.rule)
            if rule['source'] != args.source:
                p.error('Источник правила и --source должны совпадать')
            samples = {name: parse_sample(path.read_text(encoding='utf-8'), rule)
                       for name, path in [('before', args.baseline), ('after', args.current)]}
            findings += compare_samples(samples['before'], samples['after'])
        result = {'source': args.source, 'assessed_at': time.time(),
                  'mode': 'historical_replay' if args.replay else 'live' if args.live else 'saved_evidence',
                  'snapshot_observed_at': (snapshot or {}).get('observed_at_epoch'),
                  'findings': findings, 'diagnosis': None,
                  'model_state': 'not_requested', 'samples': samples}
        if args.model and any(f['severity'] != 'info' for f in findings):
            try:
                result['diagnosis'] = diagnose(ResponsesClient(args.model, output, max_calls=1), findings, samples)
                result['model_state'] = 'completed'
            except (ModelError, ValueError) as exc:
                result['model_state'] = 'blocked'
                result['model_error'] = str(exc)
        write_json(output / 'health' / (stamp + '.json'), result)
        title = 'Диагностика Sensoren' if args.source == 'sensoren' else 'Диагностика ' + args.source
        summary = 'Архивная проверка на дату снимка; не текущее состояние.' if args.replay else 'Проверка имеющихся сигналов; действия по восстановлению не выполнялись.'
        summary += ' Модель: ' + result['model_state'] + '.'
        if result.get('model_error'):
            summary += ' ' + result['model_error']
        if result['diagnosis']:
            summary += ' Гипотеза модели: ' + result['diagnosis']['result']['summary']
        write_report(output / 'health.html', title, summary, findings=findings)
        print(json.dumps({'findings': findings, 'model_state': result['model_state'],
                          'report': str(output / 'health.html')}, ensure_ascii=False))
        return 2 if any(f['severity'] in ('critical', 'unknown') for f in findings) or result['model_state'] == 'blocked' else 0
    if args.command == 'probe':
        rule = read_json(args.rule)
        sample, findings = probe_card(rule)
        samples = {'after': sample} if sample else {}
        if sample:
            write_json(output / 'controls' / (stamp + '.json'), sample)
            if args.baseline:
                samples['before'] = read_json(args.baseline)
                findings += compare_samples(samples['before'], sample)
            else:
                findings.append(dict(code='baseline_missing', severity='info',
                    message='Контрольная страница сохранена; эталон ещё не выбран и изменение не оценено', evidence={}))
            if sample['observation']['status'] in ('parse_error', 'identity_mismatch', 'needs_variant'):
                findings.append(dict(code='control_parse_error', severity='warning',
                    message='Контрольная карточка не прошла проверку парсера',
                    evidence={'status': sample['observation']['status']}))
        result = {'findings': findings, 'diagnosis': None, 'model_state': 'not_requested'}
        if args.model and any(f['severity'] != 'info' for f in findings):
            try:
                result['diagnosis'] = diagnose(ResponsesClient(args.model, output), findings, samples)
                result['model_state'] = 'completed'
            except (ModelError, ValueError) as exc:
                result.update(model_state='blocked', model_error=str(exc))
        write_json(output / 'probes' / (stamp + '.json'), result)
        write_report(output / 'control.html', 'Контроль карточки',
                     'Отдельный запрос без записи цены в рабочую базу. Модель: ' + result['model_state'], findings=findings)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if any(f['severity'] != 'info' for f in findings) or result['model_state'] == 'blocked' else 0
    if args.command == 'extract':
        if not 1 <= args.limit <= 20:
            p.error('--limit должен быть от 1 до 20')
        paths = read_json(args.manifest)['tasks'][:args.limit]
        if not paths:
            p.error('Нет заданий в manifest')
        tasks = [read_json(path) for path in paths]
        for task in tasks:
            verify_task(task)
        client = ResponsesClient(args.model, output, max_calls=args.limit)
        records, errors = [], []
        for task in tasks:
            try:
                records.append(extract(task, client, output))
            except (ModelError, ValueError) as exc:
                errors.append({'task': task['id'], 'error': str(exc)})
                break  # Authentication/network failures must not spend the whole batch budget.
        summary = f'Предложений: {len(records)} из {len(tasks)}. Все требуют проверки. API-вызовов: {client.calls}.'
        if errors:
            summary += ' Обработка остановлена: ' + errors[0]['error']
        write_json(output / 'extraction_runs' / (stamp + '.json'),
                   {'model': args.model, 'attempted_api_calls': client.calls, 'errors': errors,
                    'proposal_ids': [r['id'] for r in records], 'tasks_requested': len(tasks)})
        write_report(output / 'documents.html', 'Пилот чтения паспортов', summary, records=records, tasks=tasks)
        print(summary)
        return 2 if errors else 0
    if args.command == 'import-result':
        task, result = read_json(args.task), read_json(args.result)
        reply = {'result': result, 'model': args.model_label, 'usage': None, 'seconds': None,
                 'created_at': time.time(), 'cache_hit': False}
        record = save_proposal(output, task, reply, origin=args.origin)
        write_report(output / ('review-' + task['id'] + '.html'), 'Предложенные характеристики',
                     'Разбор в сессии; не автономный API-прогон и не независимый тест точности.', records=[record])
        print('Предложение сохранено: ' + record['id'])
        return 0
    if args.command == 'evaluate':
        records = [read_json(path) for path in sorted((output / 'proposals').glob('*.json'))]
        records = [r for r in records if r['origin'] == args.origin and r['task']['dataset'] == args.dataset]
        # Latest proposal per input; cache hits are not new independent trials.
        records.sort(key=lambda r: r['created_at'], reverse=True)
        result = evaluate(records, read_json(args.gold)['records'])
        result['origin'] = args.origin
        result['dataset'] = args.dataset
        write_json(output / ('evaluation-' + args.origin + '.json'), result)
        print(json.dumps({k: v for k, v in result.items() if k != 'details'}, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        # Avoid printing credential-bearing paths or arbitrary input values.
        print('Ошибка входных данных или файлов: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
