"""First cloud deployment: fail-closed login and owner-only connection checks.

This entrypoint intentionally never opens the local monitoring database/files.
Collection, editorial work and data migration are the next deployment stage.
"""

import streamlit as st

from cloud.access import ROLE_LABELS, authorize, require_admin
from cloud.readiness import check_drive, check_login_config, check_neon, section


TITLE = "Конкрентная аналитика СКБ ИНДУКЦИЯ"


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


def main():
    st.set_page_config(page_title=TITLE, page_icon="📊", layout="wide")
    st.title(TITLE)
    st.caption("Публикации конкурентов · Аналитические записки · Архив отчётов")

    config = settings()
    login = check_login_config(config)
    if login.status != "unchecked":
        st.info("Приложение готовится к запуску. Вход для сотрудников пока не настроен.")
        st.caption("Администратору: завершите настройку Google-входа в Streamlit Secrets по инструкции в репозитории.")
        st.stop()

    if not st.user.is_logged_in:
        st.subheader("Вход для приглашённых сотрудников")
        st.write("Используйте Google-аккаунт, на который вам предоставили доступ.")
        if st.button("Войти через Google", type="primary"):
            try:
                st.login()
            except Exception:
                st.error("Не удалось начать вход. Администратору необходимо проверить настройки Google OAuth.")
        st.stop()

    access = authorize(identity(), section(config, "access"))
    if st.sidebar.button("Выйти"):
        # Do not retain administrator diagnostics across identities in the browser.
        st.session_state.clear()
        st.logout()
        st.stop()
    if not access.allowed:
        st.session_state.clear()
        st.warning("Доступ не предоставлен. Обратитесь к администратору или войдите другим Google-аккаунтом.")
        st.stop()

    st.sidebar.write(access.email)
    st.sidebar.caption(ROLE_LABELS[access.role])
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
    st.caption("Отключённый или отозванный доступ проверяется при каждом действии и обновлении страницы.")


if __name__ == "__main__":
    main()
