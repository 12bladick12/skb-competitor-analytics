"""A successful connection or zero search results cannot open public release."""
from datetime import datetime, timedelta, timezone
from .model import DOMAINS
from .registry import fingerprint

LOCAL=timezone(timedelta(hours=5))


def release_reasons(entities, runs, control, at=None):
    at=at or datetime.now(timezone.utc)
    reasons=[]
    if not entities or any(not e.get('fns_verified_at') or not e.get('history_verified') for e in entities):
        reasons.append('Не завершена проверка ФНС и истории юридических лиц.')
    registry=fingerprint(entities)
    required={(e['id'],d) for e in entities for d in DOMAINS}
    days=set()
    for run in runs:
        if run.get('registry') != registry or run.get('environment') != 'github_actions' or not run.get('automatic'):
            continue
        checks=run.get('checks',[])
        covered={(c['entity_id'],c['domain']) for c in checks if c.get('status')=='success'
                 and c.get('complete') and c.get('all_history') and c.get('known_cases_refreshed')}
        if required <= covered and run.get('finished_at') and run.get('status')=='success':
            started=datetime.fromisoformat(run['started_at']).astimezone(LOCAL)
            finished=datetime.fromisoformat(run['finished_at']).astimezone(LOCAL)
            if started.date()==finished.date():
                days.add(finished.date())
    # End on yesterday: a still-running current day does not invalidate 14 complete days.
    expected={at.astimezone(LOCAL).date()-timedelta(days=i) for i in range(1,15)}
    if not expected <= days:
        reasons.append('Нет 14 последовательных завершённых дней полного автоматического сбора.')
    if not control or control.get('registry') != registry or not control.get('passed') or not control.get('evidence_ids'):
        reasons.append('Не подтверждена контрольная выборка дел и документов.')
    if control and control.get('known_missing'):
        reasons.append('Есть известные пропущенные дела или события.')
    return reasons


def stale(check, at=None):
    value=check.get('last_success_at')
    if not value:
        return True
    when=datetime.fromisoformat(value)
    if when.tzinfo is None:
        when=when.replace(tzinfo=timezone.utc)
    return (at or datetime.now(timezone.utc))-when > timedelta(hours=36)


def released(state):
    return bool(state and state.get('enabled') is True and state.get('certified_at')
                and state.get('registry') and len(set(state.get('accepted_days',[]))) >= 14)
