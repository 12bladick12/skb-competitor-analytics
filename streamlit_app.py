"""Invited-user cloud library. Persistent data lives in Neon and private Drive."""

import streamlit as st

from cloud.access import ROLE_LABELS, authorize, require_admin
from cloud.public_pages import DESCRIPTION, public_links, render_public_document
from cloud.readiness import check_drive, check_login_config, check_neon, section
from cloud.storage_probe import check_drive_write, check_neon_write
from cloud.drive_store import StorageError
from cloud.screens import load_library, render_library
from cloud.presentation import apply_theme, brand, masthead, account


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
    with st.sidebar.container(key='account'):
        account(access.email, ROLE_LABELS[access.role])
        if st.button("Выйти", key='logout', width='stretch'):
            st.session_state.clear()
            st.logout()
            st.stop()
        with st.expander("О сервисе"):
            public_links()


def main():
    st.set_page_config(page_title=TITLE, page_icon="◈", layout="wide")
    apply_theme()
    masthead()
    st.title("Конкурентная аналитика")
    if render_public_document(st.query_params.get("page", "")):
        public_links()
        st.stop()

    config = settings()
    login = check_login_config(config)
    if login.status != "unchecked":
        st.info("Приложение готовится к запуску. Вход для сотрудников пока не настроен.")
        st.caption("Администратору: завершите настройку Google-входа в Streamlit Secrets по инструкции в репозитории.")
        st.stop()

    if not st.user.is_logged_in:
        st.write(DESCRIPTION)
        with st.container(key='login-panel'):
            st.subheader("Вход для приглашённых сотрудников")
            st.write("Используйте Google-аккаунт, на который вам предоставили доступ.")
            if st.button("Войти через Google", type="primary"):
                try:
                    st.login()
                except Exception:
                    st.error("Не удалось начать вход. Администратору необходимо проверить настройки Google OAuth.")
        public_links()
        st.stop()

    access = authorize(identity(), section(config, "access"))
    if not access.allowed:
        st.session_state.clear()
        st.warning("Доступ не предоставлен. Обратитесь к администратору или войдите другим Google-аккаунтом.")
        if st.sidebar.button("Выйти"):
            st.logout()
        st.stop()

    with st.sidebar:
        brand()
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
        st.subheader("Подготовка общей версии")
        st.write("Вход по приглашениям подключён. Публикации, редактор и архив появятся после переноса данных и завершения облачной версии.")

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
            require_admin(identity(), section(fresh, "access"))
        except PermissionError:
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
            require_admin(identity(), section(fresh, "access"))
        except PermissionError:
            st.error("Права изменились. Обновите страницу.")
            st.stop()
        with st.spinner("Проверяем сохранение, повторное чтение и удаление тестовых данных…"):
            show_check(check_neon_write(fresh))
            show_check(check_drive_write(fresh))
    st.caption("Отключённый или отозванный доступ проверяется при каждом действии и обновлении страницы.")


if __name__ == "__main__":
    main()
