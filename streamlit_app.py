"""Shared Streamlit shell for news (Neon/Drive) and prices (Supabase)."""

from pathlib import Path
import runpy
import streamlit as st

from cloud.access import ROLE_LABELS, current_access, current_admin
from cloud.public_pages import DESCRIPTION, public_links, render_public_document
from cloud.readiness import check_drive, check_login_config, check_neon, section
from cloud.storage_probe import check_drive_write, check_neon_write
from cloud.drive_store import StorageError
from cloud.screens import load_library, render_library
from cloud.presentation import apply_theme, brand, masthead, account
from cloud.editor import dirty, discard_editor, render_guard


TITLE = "Конкурентная аналитика СКБ ИНДУКЦИЯ"


def settings():
    try:
        return st.secrets.to_dict()
    except Exception:
        # Config errors must not expose a secret, host, allowlist or stack trace.
        return {}


def identity():
    return dict(st.user.to_dict(), is_logged_in=st.user.is_logged_in)


def show_check(result):
    presenter = {"ok": st.success, "error": st.error, "warning": st.warning}.get(result.status, st.info)
    presenter(result.message)
    if "free_bytes" in result.details:
        st.metric("Свободное место Google Drive", f"{result.details['free_bytes'] / 1e9:.2f} ГБ")


def sidebar_account(access):
    render_guard()
    with st.sidebar.container(key='account'):
        account(access.email, ROLE_LABELS[access.role])
        if st.button("Выйти", key='logout', width='stretch'):
            if dirty():
                st.session_state['confirm_logout'] = True
            else:
                st.session_state.clear()
                st.logout()
                st.stop()
        if st.session_state.get('confirm_logout'):
            st.warning('В записке есть несохранённые правки.')
            if st.button('Выйти без сохранения'):
                st.session_state.clear()
                st.logout()
                st.stop()
        with st.expander("О сервисе"):
            public_links()


def render_news():
    config = settings()
    login = check_login_config(config)
    if login.status != "unchecked":
        st.info("Приложение готовится к запуску. Вход для сотрудников пока не настроен.")
        st.caption("Администратору: завершите настройку Google-входа в Streamlit Secrets по инструкции в репозитории.")
        st.stop()

    if not st.user.is_logged_in:
        st.write(DESCRIPTION)
        with st.container(key='login-panel'):
            st.subheader("Вход и регистрация")
            st.write("Войдите через Google. При первом входе будет создана учётная запись с доступом на просмотр.")
            if st.button("Войти через Google", type="primary"):
                try:
                    st.login()
                except Exception:
                    st.error("Не удалось начать вход. Администратору необходимо проверить настройки Google OAuth.")
        public_links()
        st.stop()

    try:
        access = current_access(identity(), config, register=True)
    except (StorageError,ValueError):
        st.error('Не удалось проверить доступ. Повторите открытие страницы.')
        st.stop()
    if not access.allowed:
        st.session_state.clear()
        st.warning("Доступ заблокирован администратором." if access.status=='blocked' else "Доступ не предоставлен. Обратитесь к администратору.")
        if st.sidebar.button("Выйти"):
            st.logout()
        st.stop()

    @st.fragment(run_every=30)
    def check_membership():
        # An open tab also loses access after blocking/demotion; never trust its
        # previous role for downloads, drafts, administrative actions or jobs.
        try:
            latest=current_access(identity(),settings())
        except (StorageError,ValueError):
            st.rerun()
        if latest.role!=access.role or not latest.allowed:
            st.session_state.clear()
            st.rerun()
    check_membership()

    library = None
    if section(config, "cloud").get("database_url"):
        try:
            with st.spinner("Загружаем общие материалы…"):
                library = load_library(section(config, "cloud")["database_url"])
            if library and render_library(library, access, settings, identity):
                sidebar_account(access)
                return
        except (StorageError, ValueError):
            st.error("Не удалось загрузить общие материалы. Повторите открытие страницы; сохранённые данные не сбрасываются.")
            if access.role != 'admin':
                sidebar_account(access)
                st.stop()
    sidebar_account(access)
    if library is None:
        st.info("Материалы пока не добавлены.")

    if access.role != "admin":
        st.info("Администратор сообщит, когда материалы будут доступны.")
        st.stop()

    st.divider()
    st.subheader("Проверка подключений")
    st.write("Проверка читает состояние Neon и свободное место Drive. Она не переносит и не изменяет рабочие данные.")
    if st.button("Проверить Neon и Google Drive", type="primary"):
        # Read settings and authorize again at the action boundary, not just in UI.
        fresh = settings()
        try:
            current_admin(identity(), fresh)
        except (PermissionError,StorageError):
            st.error("Права изменились. Обновите страницу.")
            st.stop()
        with st.spinner("Проверяем подключения…"):
            show_check(check_neon(fresh))
            show_check(check_drive(fresh))
    st.subheader("Проверка сохранения данных")
    st.write("Создаёт отдельную тестовую запись в Neon и небольшой тестовый файл в Drive, "
             "читает их обратно и удаляет. Проверка не переносит материалы мониторинга.")
    if st.button("Проверить запись и чтение"):
        fresh = settings()
        try:
            current_admin(identity(), fresh)
        except (PermissionError,StorageError):
            st.error("Права изменились. Обновите страницу.")
            st.stop()
        with st.spinner("Проверяем сохранение, повторное чтение и удаление тестовых данных…"):
            show_check(check_neon_write(fresh))
            show_check(check_drive_write(fresh))
    st.caption("Отключённый или отозванный доступ проверяется при каждом действии и обновлении страницы.")


WORKSPACES = {"news": "Новости конкурентов", "prices": "Цены конкурентов"}


def select_workspace(workspace):
    st.session_state["workspace-tabs"] = WORKSPACES[workspace]
    st.session_state["_workspace_url"] = workspace
    st.query_params["workspace"] = workspace


def workspace_changed():
    chosen = st.session_state["workspace-tabs"]
    target = next(key for key, label in WORKSPACES.items() if label == chosen)
    if target == "prices" and dirty():
        st.session_state["pending_workspace"] = target
        select_workspace("news")
        return
    select_workspace(target)


def continue_to_prices():
    discard_editor()
    st.session_state.pop("pending_workspace", None)
    select_workspace("prices")


def stay_in_news():
    st.session_state.pop("pending_workspace", None)


def main():
    st.set_page_config(page_title=TITLE, page_icon="◈", layout="wide")
    apply_theme()
    masthead()
    st.title("Конкурентная аналитика")
    if render_public_document(st.query_params.get("page", "")):
        public_links()
        st.stop()
    with st.sidebar:
        brand()

    target = st.query_params.get("workspace", "news")
    if target not in WORKSPACES:
        target = "news"
    if st.session_state.get("_workspace_url") != target:
        if target == "prices" and dirty():
            st.session_state["pending_workspace"] = target
            target = "news"
        select_workspace(target)

    news, prices = st.tabs(list(WORKSPACES.values()), key="workspace-tabs", on_change=workspace_changed)
    if news.open:
        with news:
            if st.session_state.get("pending_workspace"):
                st.warning("В новостной записке есть несохранённые правки. Сохраните их перед переходом к ценам или продолжите без сохранения.")
                back, proceed = st.columns(2)
                back.button("Остаться в новостях", on_click=stay_in_news, width="stretch")
                proceed.button("Перейти к ценам без сохранения", on_click=continue_to_prices, width="stretch")
            render_news()
    if prices.open:
        with prices:
            render_guard()
            if globals().get("PRICE_CLOUD_MODE", True) and not settings().get("database") and st.query_params.get("price_section") != "sources":
                st.info("Вкладка цен подготовлена. Для подключения сохранённой истории владелец приложения должен добавить раздел [database] из настроек прежнего приложения цен в Secrets этого приложения.")
                st.caption("Существующие разделы настроек Google, Neon и Drive нужно сохранить. После подключения здесь появятся сбор, база товаров и сравнение цен.")
                return
            runpy.run_path(str(Path(__file__).parent / "price_monitor" / "ui.py"),
                           init_globals={"CLOUD_MODE": globals().get("PRICE_CLOUD_MODE", True)})


if __name__ == "__main__":
    main()
