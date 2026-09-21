import hashlib
from io import BytesIO
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from cloud.drive_store import DriveStore, StorageError
from cloud.library import Repository, catalog_id, period_events, read_asset
from cloud.screens import coverage_table, file_bytes, url


CONFIG = {"cloud": {"database_url": "postgresql://test:TEST@ep-test.neon.tech/db"}}


def sample():
    events = {}
    for ident, kind, state, code in [(1,'news','confirmed','a'), (2,'products','confirmed','b'), (3,'news','rejected','a')]:
        events[str(ident)] = dict(id=ident,kind=kind,status=state,competitor_code=code,competitor_name=code,
            title='Материал '+str(ident),original_text='Проверенный материал о датчиках',description='Описание',
            date_label='01.09.2026',url='https://example.com/news',evidence_ids=['a'*64],provenance={'date_evidence':'01.09.2026'})
    return {'id':'c'*64, 'manifest':{'source_created_at':'2026-09-18T09:00:00+00:00'}, 'event':events,
            'period':{'2026-09':{'event_ids':[1,2,3], 'checks':[]}},
            'competitor':{'a':{'code':'a','name':'Компания A','base_url':'https://example.com'},'b':{'code':'b','name':'Компания B','base_url':'https://example.org'}},
            'run':{},'draft':{},'report':{}}


class LibraryTests(unittest.TestCase):
    def test_worker_registry_uses_one_query_and_rejects_foreign_import_or_unknown_id(self):
        from cloud.library import AssetIndex
        store=MagicMock()
        store.query.return_value=[{'id':'a'*64,'payload':{'pack':'b'*64,'bytes':12}}]
        index=AssetIndex(store,'c'*64)
        self.assertEqual(index.asset('c'*64,'a'*64)['bytes'],12)
        self.assertEqual(index.asset('c'*64,'a'*64)['bytes'],12)
        store.query.assert_called_once()
        for import_id,asset_id in [('d'*64,'a'*64),('c'*64,'../secrets.toml'),('c'*64,'e'*64)]:
            with self.assertRaises(StorageError):index.asset(import_id,asset_id)
    def test_external_text_is_escaped_in_visual_cards_and_source_table(self):
        from cloud.presentation import event_card
        event = sample()['event']['1']
        event['title'] = '<img src=x onerror=alert(1)>'
        event['description'] = '<script>alert(2)</script>'
        with patch('cloud.presentation.html') as render:
            event_card(event, 'Новости')
        markup = render.call_args.args[0]
        self.assertIn('&lt;img', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertNotIn('<script>', markup)
        with patch('cloud.screens.html') as render:
            coverage_table([{'competitor_code':'<img src=x>', 'kind':'news', 'status':'success',
                             'items':0, 'url':'javascript:alert(3)', 'reason':'<script>unsafe</script>'}])
        markup = render.call_args.args[0]
        self.assertNotIn('href=', markup)
        self.assertNotIn('<script>', markup)
        self.assertIn('Проверен, публикаций нет', markup)

    def test_visual_coverage_keeps_partial_failed_and_unconfirmed_separate(self):
        from cloud.presentation import coverage_summary
        with patch('cloud.presentation.html') as render:
            coverage_summary([{'status':'success','items':0},{'status':'partial'},
                              {'status':'error'},{'status':'not_configured','kind':'telegram','url':''}])
        markup = ''.join(c.args[0] for c in render.call_args_list)
        self.assertIn('Проверено источников: 1 из 3', markup)
        self.assertIn('Из проверенных: 1 без публикаций', markup)
        for label in ('Сбор неполный', 'Ошибки', 'не входят в число источников'):
            self.assertIn(label, markup)

    def test_import_identifier_survives_json_key_normalization(self):
        self.assertEqual(catalog_id({'links':{2:'two',10:'ten'}}),catalog_id({'links':{'2':'two','10':'ten'}}))
    def test_migration_uses_same_neon_endpoint_without_pooler(self):
        config={'cloud':{'database_url':'postgresql://test:TEST@ep-test-pooler.eu-central-1.aws.neon.tech/db'}}
        runtime=Repository(config,connect=MagicMock())
        migration=Repository(config,connect=MagicMock(),migration=True)
        self.assertEqual(runtime.params['host'],'ep-test-pooler.eu-central-1.aws.neon.tech')
        self.assertEqual(migration.params['host'],'ep-test.eu-central-1.aws.neon.tech')
        self.assertNotIn('options',migration.params)

    def test_filters_never_promote_rejected_events(self):
        data = sample()
        self.assertEqual([e['id'] for e in period_events(data,'2026-09')], [1,2])
        self.assertEqual([e['id'] for e in period_events(data,'2026-09',competitor='b',kind='products',query='ДАТЧИКАХ')],[2])
        self.assertEqual(period_events(data,'2026-08'),[])
        data['event']['1']['evidence_ids']=[]
        self.assertEqual([e['id'] for e in period_events(data,'2026-09')],[2])

    def test_file_reads_require_current_invitation(self):
        with patch('cloud.screens.DriveStore') as drive, patch('cloud.screens.Repository') as repository:
            with self.assertRaises(PermissionError):
                file_bytes(lambda: {'access':{}},lambda:{},'c'*64,'a'*64)
            drive.assert_not_called()
            repository.assert_not_called()

    def test_no_paths_or_arbitrary_ids_reach_storage(self):
        connect = MagicMock()
        repository = Repository(CONFIG, connect=connect)
        for key in ('../secrets.toml', 'drive_id', 'file:///C:/private', 'https://evil.test'):
            with self.assertRaises(StorageError):
                repository.asset('c'*64,key)
        connect.assert_not_called()
        self.assertEqual(url('javascript:alert(1)'), '')
        self.assertEqual(url('https://user:pass@example.org'), '')

    def test_asset_bytes_and_integrity_and_bounded_extraction(self):
        content=b'<script>window.RUN=true</script>'
        digest=hashlib.sha256(content).hexdigest()
        stream=BytesIO()
        with ZipFile(stream,'w') as archive:
            archive.writestr(digest,content)
        repository, drive=MagicMock(),MagicMock()
        repository.asset.return_value={'drive_id':'allocated_file_id','pack':'b'*64,'bytes':len(content)}
        drive.download.return_value=stream.getvalue()
        self.assertEqual(read_asset(repository,drive,'c'*64,digest),content)
        repository.asset.return_value['bytes']=1
        with self.assertRaises(StorageError):
            read_asset(repository,drive,'c'*64,digest)

    def test_download_rejects_wrong_hash_oversize_and_redirect(self):
        session=MagicMock()
        response=session.request.return_value
        response.status_code=200
        response.iter_content.return_value=iter([b'data'])
        store=DriveStore({},session=session)
        with patch.object(store,'_headers',return_value={'Authorization':'Bearer SECRET'}):
            with self.assertRaisesRegex(StorageError,'сумма'):
                store.download('valid_file_123','0'*64)
            response.iter_content.return_value=iter([b'data'])
            with self.assertRaisesRegex(StorageError,'Размер'):
                store.download('valid_file_123','0'*64,maximum=2)
            response.status_code=302
            with self.assertRaises(StorageError):
                store.download('valid_file_123','0'*64)
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])


class LibraryScreenTests(unittest.TestCase):
    def setUp(self):
        probe=patch('cloud.job_screen.JobService.list',return_value=[])
        probe.start()
        self.addCleanup(probe.stop)
        self.members={email:{'email':email,'subject':'test-subject','status':'active','role':role}
            for email,role in [('reader@example.com','viewer'),('admin@example.com','admin')]}
        def register(email,subject):
            return self.members.setdefault(email,{'email':email,'subject':subject,'status':'active','role':'viewer'})
        for name,callback in [('find',lambda email,subject:self.members.get(email)),('register',register)]:
            member=patch('cloud.members.MemberStore.'+name,side_effect=callback)
            member.start();self.addCleanup(member.stop)
    def application(self, page, email='reader@example.com'):
        import test_cloud_streamlit as fixture
        from cloud.screens import load_library
        load_library.clear()
        helper=fixture.CloudScreenTests()
        app=helper.application()
        app.secrets['cloud'] = CONFIG['cloud']
        app.query_params['section']=page
        app.query_params['period']='2026-09'
        editor = patch('cloud.editor.DraftService.open',return_value={'draft':None,'library':sample(),'issues':[],'role':'viewer'})
        editor.start()
        self.addCleanup(editor.stop)
        return app,helper.user(email)

    def test_invited_viewer_reads_library_without_admin_controls(self):
        app,user=self.application('Обзор')
        with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()) as load:
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value,'2')
        self.assertNotIn('Подключения',app.radio[0].options)
        self.assertEqual(app.radio[0].options,['Обзор','Публикации','Конкуренты','Архив'])
        self.assertFalse(any(e.label=='Статус приложения' for e in app.expander))
        load.assert_called_once()

    def test_new_verified_user_registers_with_read_only_access(self):
        app,user=self.application('Обзор','outsider@example.com')
        with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()) as load:
            app.run()
        self.assertFalse(app.exception)
        load.assert_called_once()
        self.assertEqual(self.members['outsider@example.com']['role'],'viewer')
        self.assertNotIn('Черновики',app.radio[0].options)

    def test_proof_is_displayed_as_code_without_executing_html(self):
        app,user=self.application('Публикации')
        html=b'<script>window.UNSAFE=true</script><p>proof</p>'
        with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()), \
             patch('cloud.screens.file_bytes',return_value=html) as read:
            app.run()
            next(b for b in app.button if b.label=='Показать сохранённый HTML').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.code[0].value,html.decode())
        self.assertFalse(any('UNSAFE' in m.value for m in app.markdown))
        read.assert_called_once()

    def test_empty_archive_and_saved_drafts_are_readable(self):
        for page in ['Архив','Черновики','Конкуренты','Сбор данных']:
            app,user=self.application(page)
            with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()):
                app.run()
            self.assertFalse(app.exception)

    def test_revoked_viewer_cannot_open_previously_rendered_proof(self):
        app,user=self.application('Публикации')
        with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()), \
             patch('cloud.screens.file_bytes') as read:
            app.run()
            button=next(b for b in app.button if b.label=='Показать сохранённый HTML')
            self.members['reader@example.com']['status']='blocked'
            button.click().run()
        self.assertFalse(app.exception)
        read.assert_not_called()
        self.assertTrue(any('Доступ заблокирован' in x.value for x in app.warning))

    def test_navigation_and_filters_do_not_reset_on_every_second_rerun(self):
        app,user=self.application('Обзор','admin@example.com')
        with patch('streamlit.user',user),patch('cloud.library.Repository.load',return_value=sample()):
            app.run()
            for page,title in [('Публикации','Публикации'),('Конкуренты','Конкуренты'),('Сбор данных','Сбор данных'),
                               ('Черновики','Редактор записки'),('Архив','Архив отчётов'),('Публикации','Публикации')]:
                app.radio[0].set_value(page).run()
                self.assertEqual(app.subheader[0].value,title)
            app.selectbox(key='pub_competitor').set_value('b').run()
            app.selectbox(key='pub_kind').set_value('products').run()
            app.text_input(key='pub_query').set_value('датчиках').run()
            self.assertTrue(any('Найдено материалов: 1' in x.value for x in app.caption))
            app.radio[0].set_value('Обзор').run()
            app.radio[0].set_value('Публикации').run()
            self.assertEqual(app.selectbox(key='pub_competitor').value,'b')
            self.assertEqual(app.text_input(key='pub_query').value,'датчиках')
        self.assertFalse(app.exception)
