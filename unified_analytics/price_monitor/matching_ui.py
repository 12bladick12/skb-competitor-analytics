"""Price comparison with the agreed compact layout and explicit match evidence."""
from collections import Counter
from datetime import date, timedelta
from html import escape
import math

import streamlit as st

from .comparison import series, metrics
from .exchange import xlsx_bytes, csv_bytes
from .matching import VERSION, STATUS_LABELS, FIELD_LABELS, display, load_catalog, evaluate, matching_export
from .matching_normalize import normalize_sensor
from .notation_ui import render_decoding
from .presentation import heading, money, metric_html, price_chart
from .sources import SOURCES
from .models import STATUS_LABELS as PRICE_STATES
from .analytics_ui import render_summary, price_conditions, BASES
from .price_terms import price_views, terms_for, comparable
from .product_labels import display_article
from .comparison_groups import difference_view

PROFILES={'auto':'По указанному исполнению','general':'Общее применение','cold':'Холодный климат','hot':'Высокая температура'}


@st.cache_data(show_spinner=False,max_entries=128,ttl=900)
def suggestions(record,profile,version=VERSION):
    _,matcher=load_catalog()
    reference=normalize_sensor(record)
    return reference,matcher.suggest(reference,profile)


def reference_price(item,match,matcher):
    """An old price belongs only to its saved model, never to a new suggestion."""
    stored=matcher.resolve(item.get('our_article'))
    if match and match.status not in ('incompatible','unsupported') and stored and stored.id==match.candidate.id:return item.get('our_price')
    return None


def match_badge(status,proposed=False):
    label=STATUS_LABELS.get(status,'Требует проверки')
    st.markdown(f'<span class="match-badge match-{escape(status)}">{escape(label)}</span>'+('<span class="match-proposed">Предложение</span>' if proposed else ''),unsafe_allow_html=True)


def comparison_row(item,reference,match,options,history,profile,library,matcher,basis='internet'):
    rid=item['rule_id'];saved=matcher.resolve(item.get('our_article'))
    price=reference_price(item,match,matcher)
    currency_ours=item.get('our_currency') or 'RUB'
    proposed=bool(match and (not saved or saved.id!=match.candidate.id))
    with st.container(border=True,key=f'compare_card_{rid}'):
        info,chart,competitor,ours=st.columns([2.8,3.1,1.5,1.5],vertical_alignment='center')
        with info:
            st.markdown(f'<div class="row-meta">{escape(item["manufacturer"])} · {escape(SOURCES[item["source"]].label)}</div><div class="row-title">{escape(display_article(item))}</div>',unsafe_allow_html=True)
            if match:
                st.markdown(f'<div class="match-arrow">↓ СКБ Индукция</div><div class="row-title match-our-model">{escape(match.candidate.model)}</div>',unsafe_allow_html=True)
                match_badge(match.status,proposed)
                if match.missing:st.caption(f'Уточнить характеристик: {len(match.missing)}')
                elif match.differences:st.caption(f'Отличий: {len(match.differences)} · приоритет {match.priority or "—"}')
            else:
                if item.get('our_article'):st.caption('Ручная связь: '+item['our_article'])
                match_badge('unsupported' if reference.family not in (None,'inductive') else 'review')
            st.markdown(f'[История модели](?workspace=prices&price_section=history&model={rid})')
            if item.get('automatic_attributes_count'):
                st.caption(f"Автоматически дозаполнено: {item['automatic_attributes_count']}")
        data=[x for x in history if x['rule_id']==rid]
        currencies=list(dict.fromkeys(x['currency'] for x in data if x['status']=='priced' and x.get('currency')))
        currency=currencies[-1] if currencies else item.get('last_currency') or 'RUB'
        if len(currencies)>1:
            with competitor:currency=st.selectbox('Валюта графика',currencies,index=len(currencies)-1,key=f'currency_{rid}')
        points=series(data,currency)
        from .price_analytics import model_statistics
        stats=model_statistics(item,reference,[r for r in data if r.get('currency')==currency],basis,price,bool(match and match.status=='direct' and not proposed))
        latest,change,gap=stats['price'],stats['change'],stats['gap']
        conditions=item.get('_price_terms') or {}
        ours_views=price_views(price,conditions.get('ours') or {})
        baseline=ours_views[basis] if gap is not None else None
        if basis!='internet':
            points=series([{**r,'price':price_views(r.get('price'),terms_for(r))[basis]} for r in data],currency)
        with chart:price_chart(points,baseline,height=130)
        with competitor:
            movement=(('Рост' if change>0 else 'Снижение' if change<0 else 'Без изменения')+f' · {abs(change):.1f}% за период' if change is not None else 'Одна цена или нет наблюдений')
            metric_html('Конкурент · '+BASES[basis],money(latest,currency),movement,change)
            st.caption('С НДС: '+money(stats['gross'],currency)+' · без НДС: '+money(stats['net'],currency))
            if points:st.caption(points[-1]['date'].strftime('%d.%m.%Y · UTC'))
            if data and data[-1]['status']!='priced':st.caption(PRICE_STATES.get(data[-1]['status'],data[-1]['status']))
        with ours:
            difference=difference_view(stats['delta'],gap)
            detail=f'Конкурент {difference["direction"].lower()} · {money(difference["amount"],currency)} · {difference["percent"]:.1f}% к СКБ' if gap is not None else ('Разные валюты' if price and currency_ours!=currency else 'Для разницы нужны прямой аналог и подтверждённые условия цен')
            metric_html('СКБ ИНДУКЦИЯ · '+BASES[basis],money(ours_views[basis],currency_ours),detail,gap)
            st.caption('С НДС: '+money(ours_views['gross'],currency_ours)+' · без НДС: '+money(ours_views['net'],currency_ours))
            if proposed:st.caption('Сопоставление ещё не сохранено')
            if item.get('our_price') and price is None:st.caption('Прежний ориентир: '+money(item['our_price'],currency_ours)+' · к другой или непроверенной модели')
        price_conditions(item,library)
        with st.expander('Характеристики и варианты подбора',expanded=False):
            render_decoding(reference)
            if match:
                for note in match.notices:st.info(note)
                st.caption('Назначение: '+PROFILES.get(match.profile,match.profile)+'. Результат относится к сравнению цен; для установки нужны условия применения.')
                if match.fields:st.dataframe(match.rows(),hide_index=True,width='stretch')
                if match.candidate.source_row:
                    catalog,_=load_catalog()
                    st.link_button('Наша карточка в базе номенклатуры',catalog['source_url']+f'#gid=1326881636&range=A{match.candidate.source_row}:H{match.candidate.source_row}')
            elif reference.family not in (None,'inductive'):
                st.info('Сейчас автоматический подбор настроен для индуктивных датчиков. Для этого типа правила будут добавлены отдельно.')
            else:
                st.info('Подходящий вариант не подтверждён. Проверьте тип, характеристики конкурента и обязательные условия; неизвестные значения не считаются совпадением.')
            if item.get('our_article') and not saved:st.caption('Сохранённое обозначение нашей модели не найдено однозначно в справочнике. Оно сохранено как ручная связь.')
            if options:
                counts=Counter(m.status for m in options)
                st.caption('Варианты: '+ ' · '.join(f'{STATUS_LABELS[s]}: {counts[s]}' for s in ('direct','close','review') if counts[s]))
                st.caption('Одинаковый приоритет сохраняет альтернативы: для несопоставимых уступок произвольные веса не назначаются.')
                st.dataframe([{'Приоритет':m.priority,'Наша модель':m.candidate.model,'Результат':STATUS_LABELS[m.status],
                               'Отличия':'; '.join(FIELD_LABELS[x['key']]+': '+display(x['reference'],x['key'])+' → '+display(x['candidate'],x['key']) for x in m.differences),
                               'Нужно уточнить':'; '.join(FIELD_LABELS[x['key']] for x in m.missing)} for m in options],hide_index=True,width='stretch',height=230)
                by_id={m.candidate.id:m for m in options}
                default=match.candidate.id if match and match.candidate.id in by_id else options[0].candidate.id
                widget=f'match_choice_{rid}_{profile}'
                if st.session_state.get(widget) not in by_id:st.session_state[widget]=default
                selected=st.selectbox('Наша модель для сопоставления',list(by_id),format_func=lambda v:f'{by_id[v].candidate.model} · {STATUS_LABELS[by_id[v].status]} · приоритет {by_id[v].priority}',key=widget)
                selected_match=by_id[selected]
                # The price input is keyed to the model identity: switching models
                # cannot reuse an entered price from a different candidate.
                with st.form(f'match_save_{rid}_{selected}_{profile}'):
                    left,right=st.columns([3,1])
                    old=reference_price(item,selected_match,matcher)
                    current=left.text_input('Текущая цена выбранной модели',value=str(old or ''),placeholder='Цена необязательна',key=f'match_price_{rid}_{selected}_{profile}')
                    curr=right.selectbox('Валюта нашей цены',['RUB','USD','EUR','CNY'],index=['RUB','USD','EUR','CNY'].index(currency_ours),key=f'match_currency_{rid}_{selected}_{profile}')
                    if st.form_submit_button('Сохранить сопоставление',type='primary'):
                        try:
                            library.save_comparisons([{**item,'our_article':selected_match.candidate.model,'our_price':current,'our_currency':curr}])
                            st.toast('Модель и её цена сохранены');st.rerun()
                        except ValueError as exc:st.error(str(exc))
            with st.expander('Исходные характеристики конкурента'):
                attrs=(item.get('_specifications') or {}).get('attributes') or []
                if attrs:st.dataframe(attrs,hide_index=True,width='stretch')
                else:st.caption('В собранной карточке характеристики отсутствуют.')



def render_comparison(library,import_comparisons,downloads):
    from .group_comparison_ui import render_comparison as render_groups
    return render_groups(library,import_comparisons,downloads)
