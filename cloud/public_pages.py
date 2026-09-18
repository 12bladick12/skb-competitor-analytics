"""Public service information. No settings, identities or monitoring data are read."""

from pathlib import Path

import streamlit as st


APP_URL = "https://skb-competitor-analytics.streamlit.app/"
PRIVACY_URL = APP_URL + "?page=privacy"
TERMS_URL = APP_URL + "?page=terms"
PUBLIC_DOCUMENTS = {
    "privacy": ("Политика конфиденциальности", "PRIVACY.md"),
    "terms": ("Условия использования", "TERMS.md"),
}
DESCRIPTION = (
    "Сервис для приглашённых сотрудников СКБ ИНДУКЦИЯ: мониторинг публикаций конкурентов, "
    "подготовка аналитических записок и хранение отчётов. После входа доступна общая "
    "библиотека перенесённых материалов, общий редактор записок и архив документов. "
    "Доступны сбор по кнопке, общий редактор и выпуск Word/ZIP с сохранёнными доказательствами."
)


def public_links():
    st.markdown(f"[О сервисе]({APP_URL}) · [Политика конфиденциальности]({PRIVACY_URL}) · "
                f"[Условия использования]({TERMS_URL})")


def render_public_document(page):
    document = PUBLIC_DOCUMENTS.get(page)
    if document is None:
        return False
    title, filename = document
    st.header(title)
    # Only this fixed mapping selects documents; never open a query-supplied path.
    st.markdown((Path(__file__).parent / filename).read_text(encoding="utf-8"))
    return True
