"""Inspectable manufacturer enrichment, shared by product and comparison pages."""
from contextlib import nullcontext
import streamlit as st

from .matching import FIELD_LABELS, display

APPLICATIONS={'filled':'Дополнено из обозначения','confirmed':'Подтверждает карточку',
              'card_priority':'Оставлено значение карточки','conflict':'Противоречие; оставлена карточка',
              'card_conflict':'В карточке конфликт; не дополнено'}


def render_decoding(sensor,expanded=False):
    decoded=sensor.decoding
    if not decoded:return
    panel=st.expander('Расшифровка обозначения МЕГА-К',expanded=expanded) if expanded is not None else nullcontext()
    with panel:
        st.caption('Модель: '+decoded['model']+' · версия '+decoded['version'])
        st.link_button('Система обозначений производителя',decoded['source_url'])
        for note in decoded['notes']:st.info(note)
        rows=[]
        for name,entry in decoded['fields'].items():
            rows.append({'Характеристика':FIELD_LABELS.get(name,name),'В карточке':display(entry.get('card_value'),name),
                         'Из обозначения':display(entry['value'],name),
                         'Применение':APPLICATIONS.get(entry.get('application'),'Не применяется'),
                         'Основание':entry['token']+' · п. '+entry['section']+(' · по умолчанию' if entry['default'] else '')})
        if rows:
            st.dataframe(rows,hide_index=True,width='stretch')
            conflicts=sum(x.get('application')=='conflict' for x in decoded['fields'].values())
            filled=sum(x.get('application')=='filled' for x in decoded['fields'].values())
            st.caption(f'Дополнено характеристик: {filled}. Противоречий с явными кодами: {conflicts}. Исходная карточка сохранена.')
        if decoded['extras']:st.table([{'Дополнительно':k,'По обозначению':str(v)} for k,v in decoded['extras'].items()])
        st.caption('Шаг резьбы и максимальная частота переключения в этой системе не заданы. Частотное исполнение не определяет частоту переключения. Температурный код сам по себе не выбирает назначение «Холодный климат».')
