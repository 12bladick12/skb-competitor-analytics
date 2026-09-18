from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from zipfile import ZipFile

from docx import Document
from cloud.coverage import monitored
from cloud.draft_rules import snapshot
from cloud.exports import render
from cloud.jobs import JobService
from test_cloud_drafts import fixture, draft_fixture


class ReleaseTests(unittest.TestCase):
    def test_unconfigured_channels_are_outside_coverage_but_real_errors_remain(self):
        checks=[{'kind':'telegram','url':'','status':'not_configured'},
                {'kind':'telegram','url':'https://t.me/teko_pro','status':'error'},
                {'kind':'news','url':'https://example.com','status':'success','items':0}]
        self.assertEqual(monitored(checks),checks[1:])
        self.assertEqual(len(checks),3)

    def test_word_and_portable_bundle_use_saved_selection_and_inert_evidence(self):
        data=fixture()
        raw=b'<html><script>alert(1)</script><h1>evidence</h1></html>'
        digest=hashlib.sha256(raw).hexdigest()
        for event in data['event'].values():
            event['evidence_ids']=[digest]
        draft=draft_fixture(data)
        draft['items'][0].update(title='Ручной заголовок',description='Сохранённое описание')
        draft['items'][1]['included']=False
        draft['conclusions']='Вывод аналитика'
        saved=snapshot(draft,data)
        before=deepcopy(saved)
        word,bundle=render(saved,{digest:raw},data['competitor'])
        text='\n'.join(p.text for p in Document(BytesIO(word)).paragraphs)
        text+='\n'+'\n'.join(cell.text for table in Document(BytesIO(word)).tables for row in table.rows for cell in row.cells)
        self.assertIn('Ручной заголовок',text)
        self.assertIn('Сохранённое описание',text)
        self.assertIn('Вывод аналитика',text)
        self.assertNotIn(draft['items'][1]['title'],text)
        with ZipFile(BytesIO(bundle)) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            self.assertEqual(len(manifest['items']),1)
            self.assertEqual(manifest['counts'],saved['counts'])
            self.assertEqual(archive.read('report.docx'),word)
            proof=manifest['items'][0]['source']['evidence_files'][0]
            self.assertEqual(archive.read(proof['original']),raw)
            self.assertNotIn(b'<script>',archive.read(proof['view']))
            self.assertIn(b'sandbox',archive.read(proof['view']))
            self.assertTrue(all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in archive.namelist()))
            with tempfile.TemporaryDirectory() as folder:
                archive.extractall(folder)
                self.assertTrue((Path(folder)/proof['view']).is_file())
        self.assertEqual(saved,before)
        with self.assertRaises(ValueError):render(saved,{digest:b'changed'},data['competitor'])

    def test_job_actions_reject_viewer_before_storage(self):
        config={'access':{'viewer_emails':['reader@example.com']}}
        claims={'is_logged_in':True,'email_verified':True,'iss':'https://accounts.google.com','sub':'1','email':'reader@example.com'}
        factory=MagicMock()
        service=JobService(lambda:config,lambda:claims,store_factory=factory)
        for action in [lambda:service.collect('c'*64,'2026-09'),lambda:service.export('c'*64,'2026-09',1),lambda:service.retry('c'*64,'x')]:
            with self.assertRaises(PermissionError): action()
        factory.assert_not_called()

    def test_collection_preserves_ids_and_uses_verified_monitor_without_invented_channels(self):
        from cloud.collection import collect
        from src.models import CompetitorConfig
        from src.verified_monitor import VerifiedMonitor
        data=fixture()
        data['event']={};data['run']={};data['period']={'2026-09':{'event_ids':[],'checks':[]}}
        company=CompetitorConfig(code='sensor',name='СЕНСОР',base_url='https://example.com',
            monitor_sources=[{'kind':'news','url':'https://example.com/news','mode':'articles'}])
        page='<html><h1>Новая публикация</h1><article><p>Подробная новость производителя для проверки облачного сбора данных и сохранения доказательств.</p></article></html>'
        def source(monitor,run_id,competitor,source,period):
            path=monitor.snapshot(competitor.code,source['url'],page)
            monitor.store.record(dict(competitor_code=competitor.code,competitor_name=competitor.name,kind='news',
                url='https://example.com/article',title='Новая публикация',original_text='Подробная новость производителя для проверки и сохранения доказательств.',
                published_at='2026-09-10T00:00:00',date_basis='publication_date',
                evidence={'official_url':competitor.base_url,'date_evidence':'10.09.2026','snapshots':[path]}))
            monitor.store.check(run_id,competitor.code,'news',source['url'],'success',items=1)
        def noop(*args):pass
        with tempfile.TemporaryDirectory() as folder, patch('cloud.collection.load_competitors',return_value=[company]), \
             patch.object(VerifiedMonitor,'collect_source',source),patch.object(VerifiedMonitor,'revalidate_saved_events',noop), \
             patch.object(VerifiedMonitor,'check_event_links',noop):
            records,objects,result=collect(data,'2026-09',Path(folder),lambda _:b'',noop)
        self.assertEqual(result['status'],'success')
        event=next(r['payload'] for r in records if r['kind']=='event')
        period=next(r['payload'] for r in records if r['kind']=='period')
        self.assertEqual(event['id'],1)
        self.assertEqual(period['event_ids'],[1])
        self.assertEqual(len(period['checks']),1)
        self.assertEqual(period['checks'][0]['kind'],'news')
        self.assertTrue(all(hashlib.sha256(v).hexdigest()==k for k,v in objects.items()))
