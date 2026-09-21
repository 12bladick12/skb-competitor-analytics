"""Administrative user list; blocked identities remain as a deny record."""
import streamlit as st
from math import ceil
from .access import ROLE_LABELS
from .drive_store import StorageError
from .members import MemberService


def render_members(settings, identity):
    st.subheader('Пользователи')
    st.caption('Новые пользователи получают доступ на просмотр. Редактирование и сбор доступны редакторам и администраторам.')
    service=MemberService(settings,identity)
    try:
        rows=service.list()
    except (StorageError,PermissionError,ValueError) as exc:
        st.error(str(exc))
        return
    query=st.text_input('Найти пользователя',placeholder='Адрес электронной почты').strip().casefold()
    show_blocked=st.checkbox('Показать заблокированных')
    selected=[r for r in rows if (show_blocked or r['status']=='active') and query in r['email'].casefold()]
    st.caption(f'Пользователей: {len(selected)}')
    pages=max(1,ceil(len(selected)/25))
    page=st.number_input('Страница списка',min_value=1,max_value=pages,value=1,step=1) if pages>1 else 1
    for row in selected[(page-1)*25:page*25]:
        with st.container(border=True):
            st.write(row['email'])
            st.caption(ROLE_LABELS[row['role']]+' · '+('Доступ открыт' if row['status']=='active' else 'Заблокирован'))
            if row['protected'] or row['email']==identity().get('email','').strip().lower():
                st.caption('Учётная запись администратора защищена от удаления здесь.')
                continue
            with st.form('member_'+row['email']+'_'+str(row['revision'])):
                role=st.selectbox('Роль',list(ROLE_LABELS),index=list(ROLE_LABELS).index(row['role']),format_func=ROLE_LABELS.get)
                blocked=st.checkbox('Заблокировать доступ',value=row['status']=='blocked')
                st.caption('Блокировка удаляет пользователя из активного списка и запрещает повторный вход с этой учётной записью.')
                if st.form_submit_button('Сохранить',type='primary'):
                    try:
                        service.change(row['email'],row['revision'],role,'blocked' if blocked else 'active')
                        st.rerun()
                    except (StorageError,PermissionError,ValueError) as exc:
                        st.error(str(exc))
