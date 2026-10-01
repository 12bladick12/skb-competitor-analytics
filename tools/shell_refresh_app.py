"""Synthetic slow refresh for verify_shell.py; never connects to storage."""
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import streamlit as st
from cloud.presentation import apply_theme

st.set_page_config(layout='wide')
apply_theme()
st.title('Проверка обновления')


@st.fragment(run_every=4)
def refresh():
    time.sleep(2)
    st.write('Сохранённые данные')
    st.dataframe({'Товар':['Датчик A','Датчик B'],'Цена':[1200,3400]})
    with st.expander('Характеристики',expanded=True):
        st.write('Данные остаются читаемыми при обновлении')


refresh()
