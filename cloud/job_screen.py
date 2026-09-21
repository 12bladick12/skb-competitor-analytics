"""Authorized queue controls; poll only while an actual job is pending."""
import streamlit as st

from .jobs import JobService
from .drive_store import StorageError
from .runtime import ensure_worker

LABELS={'queued':'Ожидание','running':'Выполняется','success':'Завершено','partial':'Завершено частично',
        'error':'Ошибка','interrupted':'Прервано'}


def launch(settings):
    ensure_worker(settings())


def render_jobs(import_id, settings, identity, can_write, *, period=None):
    service=JobService(settings,identity)
    try:
        rows=service.list(import_id)
    except (StorageError,PermissionError) as exc:
        st.error(str(exc))
        return
    pending=any(r['status'] in ('queued','running') for r in rows)
    if pending:
        launch(settings)

    @st.fragment(run_every=2 if pending else None)
    def status():
        from .screens import local_time, load_library
        try:
            current=service.list(import_id)
        except (StorageError,PermissionError) as exc:
            st.error(str(exc))
            return
        active=any(r['status'] in ('queued','running') for r in current)
        if active:
            launch(settings)
        if pending and not active:
            load_library.clear()
            st.rerun()
        selected=[r for r in current if not period or r['period']==period]
        for row in selected[:10]:
            title=('Выпуск Word/PDF/ZIP' if row['kind']=='export' else 'Сбор данных')+' · '+row['period']
            with st.container(border=True):
                st.markdown('**'+title+' — '+LABELS[row['status']]+'**')
                st.caption(local_time(str(row['created_at']))+' · '+row['stage'])
                if row['kind']=='collect':
                    st.write(f"Проверено источников: {row['completed']}")
                    if row['source']:
                        st.caption('Текущий источник: '+row['source'])
                if row['error']:
                    st.error(row['error'])
                if row['kind']=='export' and row['status']=='success':
                    st.success('Word, PDF и ZIP сохранены в архиве.')
                if can_write and row['status'] in ('error','interrupted') and st.button('Повторить запуск',key='retry_'+row['id']):
                    try:
                        service.retry(import_id,row['id'])
                        launch(settings)
                        st.rerun()
                    except (StorageError,PermissionError,ValueError) as exc:
                        st.error(str(exc))
        if not selected:
            st.caption('Заданий пока нет.')
    status()
