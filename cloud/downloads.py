"""Deferred, one-click downloads. Callbacks do not access Streamlit session state."""
from copy import deepcopy
import time

from .drive_store import StorageError
from .jobs import JobService
from .library import Repository


FORMATS = (
    ('Word', 'docx_id', '.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ('PDF', 'pdf_id', '.pdf', 'application/pdf'),
    ('ZIP с доказательствами', 'bundle_id', '.zip', 'application/zip'),
)


def asset_download(settings, identity, import_id, asset_id):
    # Capture credentials in the authenticated page thread. Authorization is still
    # rechecked against current membership by file_bytes when the user clicks.
    config, claims = deepcopy(settings()), deepcopy(identity())
    def download():
        from .screens import file_bytes
        return file_bytes(lambda: config, lambda: claims, import_id, asset_id)
    return download


def release_download(settings, identity, import_id, period, revision, field, start_worker, draft_id=None):
    config, claims = deepcopy(settings()), deepcopy(identity())
    def download():
        service = JobService(lambda: config, lambda: claims)
        job = service.export(import_id, period, revision, draft_id=draft_id)
        deadline = time.monotonic() + 240
        while job['status'] in ('queued', 'running'):
            start_worker({key: dict(config.get(key, {})) for key in ('cloud', 'drive')})
            if time.monotonic() >= deadline:
                raise StorageError('Выпуск продолжается. Документ появится в архиве; повторное скачивание не создаст дубликат.')
            time.sleep(1)
            store, _ = service.context(write=True)
            rows = store.query('SELECT id,status,result_id,error FROM skb_analytics.jobs WHERE import_id=$1 AND id=$2',
                               [import_id, job['id']])
            if not rows:
                raise StorageError('Задание не найдено. Обновите страницу.')
            job = rows[0]
        if job['status'] != 'success':
            raise StorageError(job.get('error') or 'Не удалось выпустить документ. Повторите задание в разделе «Сбор данных».')
        service.context(write=True)
        library = Repository(config).load()
        if not library or library['id'] != import_id:
            raise StorageError('Набор материалов изменился. Обновите страницу.')
        report = library['report'].get(job['result_id'], {})
        if not report.get(field):
            raise StorageError('Файл выпуска не найден.')
        return asset_download(lambda: config, lambda: claims, import_id, report[field])()
    return download
