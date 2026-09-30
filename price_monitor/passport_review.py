"""Human approval is per full execution, never automatically propagated by series."""
import json
import uuid
from .models import utcnow
from .passport_recognition import validate


def approve(repository, rule_id, fingerprint, fields, reviewer, applicability_confirmed=False):
    reviewer=str(reviewer).strip()
    if not reviewer:raise ValueError('Укажите проверившего сотрудника')
    rows=repository.batch('''SELECT f.page_count,p.current_fingerprint,l.applicability FROM passport_files f
        JOIN passport_links l ON l.fingerprint=f.fingerprint JOIN passport_products p ON p.rule_id=l.rule_id
        WHERE l.rule_id=%(id)s AND f.fingerprint=%(fp)s''',{'id':rule_id,'fp':fingerprint})
    if not rows or rows[0]['current_fingerprint']!=fingerprint:raise ValueError('Паспорт больше не является текущей редакцией')
    if not applicability_confirmed:
        raise ValueError('Подтвердите применимость документа к этому полному исполнению')
    result=validate({'fields':fields,'notes':''},rows[0]['page_count'])
    names=[f['name'] for f in result['fields']]
    if len(names)!=len(set(names)):raise ValueError('Выберите одно подтверждённое значение для каждой характеристики')
    p={'id':rule_id,'fp':fingerprint,'json':json.dumps(result['fields'],ensure_ascii=False),'reviewer':reviewer,
       'now':utcnow(),'event':str(uuid.uuid4())}
    # Check current fingerprint again inside the write transaction.
    guard='EXISTS(SELECT 1 FROM passport_products WHERE rule_id=%(id)s AND current_fingerprint=%(fp)s)'
    repository.batch([f'''UPDATE passport_links SET applicability='confirmed',evidence='Проверено сотрудником: '||%(reviewer)s
        WHERE rule_id=%(id)s AND fingerprint=%(fp)s AND {guard}''',
        f'''INSERT INTO passport_field_reviews(rule_id,fingerprint,fields_json,reviewer,updated_at)
        SELECT %(id)s,%(fp)s,%(json)s,%(reviewer)s,%(now)s WHERE {guard}
        ON CONFLICT(rule_id,fingerprint) DO UPDATE SET fields_json=excluded.fields_json,reviewer=excluded.reviewer,updated_at=excluded.updated_at''',
        f'''INSERT INTO product_events(id,rule_id,kind,detail,created_at)
        SELECT %(event)s,%(id)s,'geometry_review',%(reviewer)s||': '||%(json)s,%(now)s WHERE {guard}'''],p)


def confirmed(repository, ids):
    if not ids:return {}
    p={f'id{i}':rid for i,rid in enumerate(ids)}
    rows=repository.batch('''SELECT r.rule_id,r.fingerprint,r.fields_json,r.reviewer,r.updated_at
        FROM passport_field_reviews r JOIN passport_products p ON p.rule_id=r.rule_id AND p.current_fingerprint=r.fingerprint
        WHERE EXISTS(SELECT 1 FROM passport_links l WHERE l.rule_id=r.rule_id AND l.fingerprint=r.fingerprint AND l.applicability='confirmed')
        AND r.rule_id IN ('''+','.join(f'%(id{i})s' for i in range(len(ids)))+')',p)
    return {r['rule_id']:{'fingerprint':r['fingerprint'],'fields':json.loads(r['fields_json']),
                         'reviewer':r['reviewer'],'updated_at':r['updated_at']} for r in rows}
