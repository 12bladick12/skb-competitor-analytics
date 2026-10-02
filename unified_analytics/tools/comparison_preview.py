"""Local-only preview: no cloud credentials, schedulers or production writes."""
from pathlib import Path
import sys
import json

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import streamlit as st
from cloud.presentation import apply_theme, brand
from price_monitor.presentation import CSS
from price_monitor.storage import Store
from price_monitor.library import Library
from price_monitor.preview_library import PreviewLibrary
from price_monitor.group_comparison_ui import render_comparison

st.set_page_config(page_title='Предпросмотр · Сравнение цен',page_icon='₽',layout='wide',initial_sidebar_state='collapsed')
apply_theme()
st.html(CSS)
with st.sidebar:
    brand()
    st.markdown('**Предпросмотр изменений**')
    st.caption('Локальная копия базы. Публикация после согласования.')
    st.radio('Раздел',['Сравнение цен'],label_visibility='collapsed')
    st.divider()
    st.caption('Прайс СКБ · 01.10.2026\n\nИсходные цены без НДС\n\nРасчётная ставка · 22%')

@st.cache_resource
def library(snapshot_version):
    repository=Store(ROOT/'data/comparison_preview/prices-v4.sqlite3').catalog
    path=ROOT/'data/comparison_preview/competitors.json'
    if path.exists():return PreviewLibrary(repository,json.loads(path.read_text(encoding='utf-8')))
    return Library(repository)

snapshot_path=ROOT/'data/comparison_preview/competitors.json'
version=snapshot_path.stat().st_mtime_ns if snapshot_path.exists() else 0
preview=library(version)
if isinstance(preview,PreviewLibrary):
    st.caption(f"Предпросмотр: {len(preview.snapshot['products'])} реальных карточек конкурентов. Снимок последних полученных цен; полная история остаётся в рабочей базе.")
    if 'catalog_search_selection' not in st.session_state:
        example=next((r for r in preview.snapshot['products'] if r['article']=='LR18XBF08DPOY-E2'),None)
        if example:st.session_state['catalog_search_selection']=['competitor:'+str(example['rule_id'])]
render_comparison(preview)
