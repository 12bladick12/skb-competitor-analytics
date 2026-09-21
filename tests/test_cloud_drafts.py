from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from cloud.draft_rules import DraftConflict, create_payload, issues, refresh_payload, save_payload, snapshot
from cloud.drafts import DraftService


def fixture():
    from test_cloud_library import sample
    data = sample()
    for event in data['event'].values():
        event['version'] = str(event['id']) * 64
        event['date_basis'] = 'publication_date'
    return data


def draft_fixture(data=None):
    data = data or fixture()
    return dict(id='d'*32, period='2026-09', revision=1, updated_at='2026-09-18T09:00:00+00:00',
                updated_by='editor@example.com', **create_payload(data, '2026-09'))


class MemoryStore:
    def __init__(self, data=None, draft=None):
        self.data = data or fixture()
        self.draft = deepcopy(draft)
        self.repository = MagicMock()
        self.repository.load.side_effect = lambda: deepcopy(self.data)
        self.versions = {draft['revision']:deepcopy(draft)} if draft else {}
    def read(self, *_): return deepcopy(self.draft)
    def asset_ids(self, *_): return {'a'*64}
    def create(self, import_id, period, payload, email):
        if not self.draft:
            self.draft = dict(id='d'*32, period=period, revision=1, updated_at='2026-09-18T10:00:00+00:00', updated_by=email, **deepcopy(payload))
            self.versions[1] = deepcopy(self.draft)
        return self.read()
    def commit(self, import_id, draft, revision, payload, email):
        if self.draft['revision'] != revision:
            raise DraftConflict('Черновик изменён другим сотрудником')
        self.draft = dict(id=draft['id'], period=draft['period'], revision=revision+1,
                          updated_at='2026-09-18T11:00:00+00:00', updated_by=email, **deepcopy(payload))
        self.versions[revision+1] = deepcopy(self.draft)
        return self.read()
    def history(self, *_):
        return [{k:d[k] for k in ('revision','updated_at','updated_by')} for d in reversed(list(self.versions.values()))]
    def version(self, import_id, draft_id, revision): return deepcopy(self.versions[revision])


class DraftRulesTests(unittest.TestCase):
    def test_create_and_refresh_only_add_confirmed_and_preserve_manual_work(self):
        data = fixture()
        draft = draft_fixture(data)
        self.assertEqual([i['event_id'] for i in draft['items']], [1,2])
        draft['items'][0].update(title='Ручной заголовок',description='Ручной текст',included=False)
        draft['conclusions'] = 'Мои выводы'
        new = deepcopy(data['event']['1'])
        new.update(id=4, version='4'*64)
        data['event']['4'] = new
        data['period']['2026-09']['event_ids'].append(4)
        refreshed = refresh_payload(draft,data,{'a'*64})
        self.assertEqual(refreshed['items'][:2],draft['items'])
        self.assertEqual(refreshed['conclusions'],'Мои выводы')
        self.assertEqual(refreshed['items'][2]['event_id'],4)
        self.assertEqual(len(refresh_payload({**draft,**refreshed},data)['items']),3)

    def test_save_cannot_edit_immutable_fields_or_inject_materials(self):
        data, draft = fixture(), draft_fixture()
        change = {'event_id':1,'included':False,'title':'Ручной текст','description':'Описание'}
        saved = save_payload(draft,data,'Выводы',[change])
        self.assertEqual(saved['items'][0]['source'],draft['items'][0]['source'])
        self.assertNotEqual(saved['items'][0]['title'],draft['items'][0]['title'])
        for edits in [[{**change,'url':'https://evil.test'}], [{**change,'event_id':9}], [change,change], [{**change,'included':1}], [{**change,'title':''}]]:
            with self.assertRaises(ValueError): save_payload(draft,data,'',edits)

    def test_changed_fact_requires_exact_acknowledgement_without_overwriting_editorial_text(self):
        data, draft = fixture(), draft_fixture()
        data['event']['1'].update(title='Новый первоисточник', version='f'*64)
        self.assertTrue(issues(draft,data)[0]['can_accept'])
        with self.assertRaises(DraftConflict): snapshot(draft,data)
        change = {'event_id':1,'included':True,'title':'Моя редактура','description':'Моё описание','accept_version':'f'*64}
        payload = save_payload(draft,data,'',[change])
        self.assertEqual(payload['items'][0]['title'],'Моя редактура')
        self.assertEqual(payload['items'][0]['source']['title'],'Новый первоисточник')
        self.assertEqual(snapshot({**draft,**payload},data)['counts'],{'news':1,'products':1})
        data['event']['1']['version']='e'*64
        with self.assertRaises(DraftConflict): save_payload(draft,data,'',[change])

    def test_missing_proof_revoked_status_and_period_change_block_preview(self):
        for mutate in [lambda d:d['event']['1'].update(status='rejected'),
                       lambda d:d['event']['1'].update(evidence_ids=[]),
                       lambda d:d['period']['2026-09']['event_ids'].remove(1)]:
            data,draft=fixture(),draft_fixture()
            mutate(data)
            self.assertFalse(issues(draft,data)[0]['can_accept'])
            with self.assertRaises(DraftConflict): snapshot(draft,data)
            draft['items'][0]['included']=False
            self.assertEqual(len(snapshot(draft,data)['items']),1)
        with self.assertRaises(DraftConflict): snapshot(draft_fixture(),fixture(),set())

    def test_selection_does_not_rewrite_coverage_and_preview_uses_saved_copy(self):
        data,draft=fixture(),draft_fixture()
        data['period']['2026-09']['checks']=[{'status':'partial'}]
        draft['items'][1]['included']=False
        result=snapshot(draft,data)
        self.assertEqual(result['counts'],{'news':1})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['checks'],data['period']['2026-09']['checks'])
        result['items'][0]['title']='Mutation'
        self.assertNotEqual(draft['items'][0]['title'],'Mutation')


class DraftServiceTests(unittest.TestCase):
    def setup_service(self, role='editor'):
        self.config={'access':{role+'_emails':['user@example.com']}}
        self.claims={'email':'user@example.com','email_verified':True,'is_logged_in':True,'iss':'https://accounts.google.com','sub':'u'}
        self.store=MemoryStore(draft=draft_fixture())
        self.factory=MagicMock(return_value=self.store)
        return DraftService(lambda:self.config,lambda:self.claims,self.factory)

    def test_roles_are_checked_at_every_operation_before_storage(self):
        service=self.setup_service('viewer')
        service.open('c'*64,'2026-09')
        for call in [lambda:service.create('c'*64,'2026-09'),lambda:service.refresh('c'*64,'2026-09',1),
                     lambda:service.save('c'*64,'2026-09',1,'',[])]:
            self.factory.reset_mock()
            with self.assertRaises(PermissionError):call()
            self.factory.assert_not_called()
        self.config['access']={}
        self.factory.reset_mock()
        with self.assertRaises(PermissionError):service.open('c'*64,'2026-09')
        self.factory.assert_not_called()

    def test_second_editor_cannot_overwrite_and_history_is_frozen(self):
        service=self.setup_service()
        saved=service.save('c'*64,'2026-09',1,'First editor',[])
        self.assertEqual(saved['revision'],2)
        with self.assertRaises(DraftConflict):service.save('c'*64,'2026-09',1,'Second editor',[])
        self.assertEqual(self.store.draft['conclusions'],'First editor')
        self.assertEqual(service.history('c'*64,'2026-09',1)['conclusions'],'')
        self.assertEqual(service.history('c'*64,'2026-09',2)['conclusions'],'First editor')

    def test_preview_refresh_and_save_require_fresh_active_import(self):
        service=self.setup_service()
        for call in [lambda:service.preview('b'*64,'2026-09',1),lambda:service.refresh('b'*64,'2026-09',1),
                     lambda:service.save('b'*64,'2026-09',1,'',[])]:
            with self.assertRaises(DraftConflict):call()
        self.store.data['event']['1']['status']='rejected'
        with self.assertRaises(DraftConflict):service.preview('c'*64,'2026-09',1)


class DraftScreenTests(unittest.TestCase):
    def setUp(self):
        from test_cloud_streamlit import CloudScreenTests
        self.helper=CloudScreenTests()
        self.app=self.helper.application({'editor_emails':['editor@example.com'],'viewer_emails':['reader@example.com']})
        self.app.secrets['cloud']={'database_url':'postgresql://test:TEST@ep-test.neon.tech/db'}
        self.app.query_params.update(section='Черновики',period='2026-09')
        self.store=MemoryStore(draft=draft_fixture())
        self.user=self.helper.user('editor@example.com')
        def member(email,subject):
            return {'email':email,'subject':subject,'status':'active','role':'editor' if email=='editor@example.com' else 'viewer'}
        self.patches=[patch('streamlit.user',self.user),patch('cloud.library.Repository.load',side_effect=lambda:deepcopy(self.store.data)),
                      patch('cloud.members.MemberStore.find',side_effect=member),
                      patch('cloud.drafts.DraftStore.read',side_effect=self.store.read),patch('cloud.drafts.DraftStore.asset_ids',side_effect=self.store.asset_ids),
                      patch('cloud.drafts.DraftStore.commit',side_effect=self.store.commit),patch('cloud.drafts.DraftStore.create',side_effect=self.store.create),
                      patch('cloud.drafts.DraftStore.history',side_effect=self.store.history),patch('cloud.drafts.DraftStore.version',side_effect=self.store.version)]
        from cloud.screens import load_library
        load_library.clear()
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def test_edit_save_reopen_preview_and_history(self):
        app=self.app.run()
        next(x for x in app.text_input if x.label=='Заголовок').set_value('Новый заголовок').run()
        next(x for x in app.text_area if x.label=='Выводы аналитика').set_value('Вывод аналитика').run()
        next(b for b in app.button if b.label=='Сохранить правки').click().run()
        self.assertEqual(self.store.draft['revision'],2)
        self.assertEqual(self.store.draft['items'][0]['title'],'Новый заголовок')
        app.radio(key="nav_page").set_value('Обзор').run()
        app.radio(key="nav_page").set_value('Черновики').run()
        self.assertEqual(next(x for x in app.text_input if x.label=='Заголовок').value,'Новый заголовок')
        next(b for b in app.button if b.label=='Показать сохранённую записку').click().run()
        self.assertTrue(any('Сохранённая редакция 2' in x.value for x in app.caption))
        next(b for b in app.button if b.label=='Показать историю редакций').click().run()
        self.assertEqual(len(self.store.versions),2)
        self.assertFalse(app.exception)

    def test_unsaved_navigation_and_conflict_preserve_work(self):
        app=self.app.run()
        next(x for x in app.text_input if x.label=='Заголовок').set_value('Несохранённый заголовок').run()
        app.radio(key="nav_page").set_value('Обзор').run()
        self.assertTrue(any('несохранённые' in x.value for x in app.warning))
        next(b for b in app.button if b.label=='Вернуться к черновику').click().run()
        self.assertEqual(next(x for x in app.text_input if x.label=='Заголовок').value,'Несохранённый заголовок')
        self.store.draft['revision']=2
        next(b for b in app.button if b.label=='Сохранить правки').click().run()
        self.assertTrue(any('Другой сотрудник' in x.value for x in app.error))
        self.assertEqual(next(x for x in app.text_input if x.label=='Заголовок').value,'Несохранённый заголовок')
        self.assertFalse(app.exception)

    def test_reader_cannot_change_a_draft(self):
        self.user.to_dict.return_value['email']='reader@example.com'
        app=self.app.run()
        self.assertNotIn('Сохранить правки',[b.label for b in app.button])
        self.assertTrue(all(x.disabled for x in app.text_input))
        self.assertFalse(app.exception)

    def test_material_groups_position_and_switching_keep_unsaved_edits(self):
        for ident,kind in [(4,'news'),(5,'telegram')]:
            event=deepcopy(self.store.data['event']['1'])
            event.update(id=ident,kind=kind,title='Заголовок '+str(ident),version=str(ident)*64)
            self.store.data['event'][str(ident)]=event
            self.store.data['period']['2026-09']['event_ids'].append(ident)
        self.store.draft=draft_fixture(self.store.data)
        app=self.app.run()
        selector=next(x for x in app.selectbox if x.label=='Материал для редактирования')
        self.assertEqual(selector.options,['Материал 1','Заголовок 4'])
        next(x for x in app.text_input if x.label=='Заголовок').set_value('Моя несохранённая правка').run()
        next(b for b in app.button if b.label=='Следующий →').click().run()
        self.assertEqual(next(x for x in app.text_input if x.label=='Заголовок').value,'Заголовок 4')
        self.assertTrue(any('Материал 2 из 2' in x.value for x in app.markdown))
        next(r for r in app.radio if r.label=='Группа материалов').set_value('telegram').run()
        self.assertEqual(next(x for x in app.selectbox if x.label=='Материал для редактирования').options,['Заголовок 5'])
        next(r for r in app.radio if r.label=='Группа материалов').set_value('news').run()
        next(b for b in app.button if b.label=='← Предыдущий').click().run()
        self.assertEqual(next(x for x in app.text_input if x.label=='Заголовок').value,'Моя несохранённая правка')
        self.assertFalse(app.exception)

