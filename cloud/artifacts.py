"""Pack immutable content into private Drive objects and return registry entries."""
import hashlib
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from .drive_store import StorageError


def upload_objects(store, drive, job, objects):
    folders=store.query("SELECT payload FROM skb_analytics.records WHERE import_id=$1 AND kind='storage' AND key='drive'",[job['import_id']])
    if not folders:
        raise StorageError('Папка хранилища ещё не настроена администратором.')
    folder=folders[0]['payload']['folder_id']
    drive.ensure_folder(folder)
    existing=store.asset_ids(job['import_id'])
    assets={}
    # One content-addressed member per pack: bounded downloads, retry-safe IDs,
    # no upload of unchanged evidence and no extraction of untrusted paths.
    for digest,content in objects.items():
        if hashlib.sha256(content).hexdigest()!=digest:
            raise ValueError('Некорректная контрольная сумма вложения.')
        if digest in existing:
            continue
        output=BytesIO()
        with ZipFile(output,'w',ZIP_DEFLATED) as archive:
            archive.writestr(digest,content)
        packed=output.getvalue()
        pack=hashlib.sha256(packed).hexdigest()
        store.pulse(job,stage='Сохранение файлов в закрытое хранилище')
        file_id=store.upload_id(job,pack,drive.allocate)
        drive.ensure_pack(file_id,folder,packed,pack)
        assets[digest]={'pack':pack,'drive_id':file_id,'bytes':len(content)}
    return assets
