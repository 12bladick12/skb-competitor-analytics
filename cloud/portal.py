"""Shared navigation cards for the application and price workspace."""
from html import escape
from base64 import b64encode

import streamlit as st


PRICE_PAGES = {
    'collect': ('Сбор цен', 'Запускайте сбор по производителям и следите за получением цен и характеристик.', '01'),
    'products': ('База товаров', 'Ищите товары, проверяйте характеристики и собирайте выборку для сравнения.', '02'),
    'compare': ('Сравнение цен', 'Изменения по брендам, функциональным группам и исполнениям. Разница с СКБ ИНДУКЦИЯ.', '03'),
    'runs': ('Запуски', 'История сборов, сохранённые результаты и причины пропусков.', '04'),
}

# Small, local vector icons: no external fonts or image requests on the landing pages.
ICONS = {
    'news': '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h3v4H8zM14 11h2m-2 4h2M8 18h8"/>',
    'prices': '<path d="M4 4v16h17M8 15l4-5 4 2 5-7"/><path d="M17 5h4v4"/>',
    'collect': '<path d="M12 3v12m-4-4 4 4 4-4M5 15v5h14v-5"/>',
    'products': '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/>',
    'compare': '<path d="M4 20h16M7 16V9m5 7V4m5 12v-5"/>',
    'runs': '<path d="M3 10a9 9 0 1 1 1 7M3 4v6h6M12 7v5l3 2"/>',
}
CARD_TOPICS = {
    'news': ('Публикации', 'События рынка', 'Аналитика'),
    'prices': ('Сбор данных', 'Номенклатура', 'Сравнение'),
    'collect': ('Производители', 'Цены и характеристики'),
    'products': ('Каталог', 'Проверка данных'),
    'compare': ('Динамика', 'НДС', 'Δ к СКБ ИНДУКЦИЯ'),
    'runs': ('История', 'Результаты и ошибки'),
}


def sources_footer():
    st.html('<div class="sources-footer"><a href="?workspace=prices&price_section=sources" target="_self">Источники и правила сбора</a>'
            '<span>СКБ ИНДУКЦИЯ · Конкурентная аналитика</span></div>')


def card(title, description, number, key, action, *args):
    with st.container(border=True, key='portal_card_' + key):
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#7a1f2b" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{ICONS[key]}</svg>'
        icon = '<img src="data:image/svg+xml;base64,' + b64encode(svg.encode()).decode() + '" alt="" width="24" height="24">'
        topics = ''.join(f'<span>{escape(topic)}</span>' for topic in CARD_TOPICS[key])
        st.html(f'<div class="portal-top"><span class="portal-icon">{icon}</span><span class="portal-number">{escape(number)}</span></div>'
                f'<h2 class="portal-title">{escape(title)}</h2>'
                f'<p class="portal-description">{escape(description)}</p>'
                f'<div class="portal-topics">{topics}</div>')
        button_title = 'базу товаров' if key == 'products' else title.lower()
        st.button('Открыть ' + button_title + ' →', key='portal_open_' + key,
                  on_click=action, args=args, width='stretch')


def render_home(select_workspace):
    st.html('<p class="page-intro">Новости рынка и цены конкурентов — в одном рабочем пространстве.</p>'
            '<div class="portal-section-label">Направления работы <span>02</span></div>')
    left, right = st.columns(2)
    with left:
        card('Новости', 'Публикации конкурентов, события рынка и аналитические материалы.',
             '01', 'news', select_workspace, 'news')
    with right:
        card('Цены', 'Сбор цен, база товаров и сравнение с продукцией СКБ ИНДУКЦИЯ.',
             '02', 'prices', select_workspace, 'prices')
    sources_footer()


def render_prices_home(go):
    st.title('Цены', anchor=False)
    st.html('<p class="page-intro">От сбора данных до сравнения сопоставимых исполнений.</p>'
            '<div class="portal-section-label">Работа с ценами <span>04</span></div>')
    for start in (0, 2):
        columns = st.columns(2)
        for column, (section, (title, description, number)) in zip(columns, list(PRICE_PAGES.items())[start:start+2]):
            with column:
                card(title, description, number, section, go, section)
    sources_footer()
