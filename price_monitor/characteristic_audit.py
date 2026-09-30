"""Read-only completeness audit, before and after documented enrichment."""
from collections import Counter, defaultdict

from .matching import MANDATORY, IMPORTANT, FIELD_LABELS
from .matching_normalize import normalize_sensor


def audit_records(records):
    buckets=defaultdict(Counter)
    issues=[]
    seen=Counter()
    for row in records:
        brand=row.get('manufacturer') or 'Не определён'
        sensor=normalize_sensor(row)
        attrs=(row.get('_specifications') or {}).get('attributes') or row.get('props') or []
        count=buckets[brand]
        count['rows']+=1
        count['without_attributes']+=not bool(attrs)
        count['supported_designation']+=bool(sensor.decoding.get('supported'))
        count['designation_review']+=bool(sensor.decoding.get('requires_review'))
        count['conflicts']+=bool(sensor.conflicts) or any(e.get('application') in ('conflict','card_conflict') for e in sensor.decoding.get('fields',{}).values())
        identity=(row.get('source',''),brand,str(row.get('article','')).strip().upper(),row.get('product_url',''))
        seen[identity]+=1
        count['duplicate_rows']+=seen[identity]>1
        required=list(dict.fromkeys((*MANDATORY,*IMPORTANT))) if sensor.family=='inductive' else []
        if required and sensor.values.get('body_type') in (None,'threaded'):required.append('pitch')
        missing=[k for k in required if sensor.values.get(k) is None]
        count['inductive']+=sensor.family=='inductive'
        count['inductive_missing']+=bool(missing)
        filled=sum(e.get('application')=='filled' for e in sensor.decoding.get('fields',{}).values())
        if missing or sensor.conflicts or sensor.decoding.get('requires_review') or not attrs:
            issues.append({'Производитель':brand,'Артикул':row.get('article'),'Ссылка':row.get('product_url',''),
                           'Исходных характеристик':len(attrs),'Дополнено по документу':filled,
                           'Не хватает для подбора':'; '.join(FIELD_LABELS.get(k,k) for k in missing),
                           'Конфликты карточки':'; '.join(FIELD_LABELS.get(k,k) for k in sorted(sensor.conflicts)),
                           'Проверить обозначение':bool(sensor.decoding.get('requires_review')),
                           'Источник правил':sensor.decoding.get('source_url','')})
    summary=[]
    for brand,count in sorted(buckets.items()):
        summary.append({'Производитель':brand,'Строк':count['rows'],'Без исходных характеристик':count['without_attributes'],
                        'Без характеристик, %':100*count['without_attributes']/count['rows'],
                        'Индуктивных':count['inductive'],'Неполных для подбора':count['inductive_missing'],
                        'Неполных индуктивных, %':100*count['inductive_missing']/count['inductive'] if count['inductive'] else None,
                        'С применимыми правилами':count['supported_designation'],'Требуют сверки обозначения':count['designation_review'],
                        'С конфликтами':count['conflicts'],'Повторных строк':count['duplicate_rows']})
    return summary,issues


def render_audit(library, query, source, brand):
    import streamlit as st
    from .exchange import xlsx_bytes
    with st.expander('Проверка заполнения и обозначений'):
        st.caption('Проверяет выбранную часть базы: пропуски исходных характеристик, конфликты и пригодность данных для подбора индуктивных датчиков. Для остальных типов проверяется наличие исходных полей; алгоритмы подбора ещё не добавлены.')
        selection=(query,source,brand)
        if st.button('Проверить характеристики выбранных товаров'):
            with st.spinner('Проверяем сохранённые характеристики…'):
                summary,issues=audit_records(library.export_products(query,source,brand))
                st.session_state['characteristic_audit']=(selection,summary,issues)
        saved=st.session_state.get('characteristic_audit')
        if saved and saved[0]==selection:
            _,summary,issues=saved
            st.dataframe(summary,hide_index=True,width='stretch')
            st.dataframe(issues[:200],hide_index=True,width='stretch')
            st.caption('Первые 200 проблемных позиций; полная проверенная выборка доступна в выгрузке. Отсутствие выявленных конфликтов не гарантирует взаимозаменяемость.')
            if summary:st.download_button('Проверка характеристик XLSX',xlsx_bytes(summary,extra_sheets={'Требуют проверки':issues}),file_name='characteristic_audit.xlsx')
