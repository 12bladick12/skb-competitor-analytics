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
    def upload(digest,content):
        if digest in existing or digest in assets:
            return
        output=BytesIO()
        with ZipFile(output,'w',ZIP_DEFLATED) as archive:
            archive.writestr(digest,content)
        packed=output.getvalue()
        pack=hashlib.sha256(packed).hexdigest()
        store.pulse(job,stage='Сохранение файлов')
        file_id=store.upload_id(job,pack,drive.allocate)
        drive.ensure_pack(file_id,folder,packed,pack)
        assets[digest]={'pack':pack,'drive_id':file_id,'bytes':len(content)}
    # One content-addressed member per pack: bounded downloads, retry-safe IDs,
    # no upload of unchanged evidence and no extraction of untrusted paths.
    for digest,content in objects.items():
        if hashlib.sha256(content).hexdigest()!=digest:
            raise ValueError('Некорректная контрольная сумма вложения.')
        if digest in existing:
            continue
        if len(content)>32*1024*1024:
            raise StorageError('Размер комплекта превышает 32 МБ. Разделите материалы на несколько выпусков.')
        # Keep every Drive request below its existing 5 MiB pack bound. A large
        # report is reassembled from verified parts without exposing Drive IDs.
        if len(content)>4*1024*1024:
            chunks=[]
            for offset in range(0,len(content),4*1024*1024):
                part=content[offset:offset+4*1024*1024]
                part_id=hashlib.sha256(part).hexdigest()
                upload(part_id,part)
                chunks.append(part_id)
            assets[digest]={'chunks':chunks,'bytes':len(content)}
        else:
            upload(digest,content)
    return assets
