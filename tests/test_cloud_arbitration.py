"""Synthetic checks for the invited-user Focus snapshot screen."""

import base64
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from cloud.access import Access
from cloud.arbitration import load_snapshot, metric_label, render_arbitration, render_metrics


SNAPSHOT = {
    'schema_version': 1, 'source': 'Контур.Фокус API 3.0',
    'as_of': '2026-10-19', 'fetched_at': '2026-10-19T12:00:00Z',
    'entities': [{
        'brand': 'Тестовая компания', 'name': 'ООО Тест', 'inn': '1234567890',
        'arbitration_metrics': {'y3Count': {'data': 7, 'isActual': True}},
        'general_cases': [{'number': '99-000/2026', 'region': '99', 'courtName': 'Тестовый суд',
                           'actions': [{'name': 'Заседание', 'date': '2026-10-18'}]}],
        'method_status': {'generalCourtCases': {'status': 'partial', 'complete': False,
                                                 'reason': 'pagination limit'}},
    }],
    'limitations': ['Нет полного архива арбитражных дел'],
}


def parts_fixture():
    raw = json.dumps(SNAPSHOT, ensure_ascii=False).encode('utf-8')
    key = '2026-10-19:2026-10-19T12:00:00Z'
    manifest = {'schema_version': 1, 'storage': 'parts-v1',
                'as_of': SNAPSHOT['as_of'], 'fetched_at': SNAPSHOT['fetched_at'],
                'sha256': hashlib.sha256(raw).hexdigest(), 'chunk_count': 2}
    middle = len(raw) // 2
    parts = [{'key': f'{key}:{index:03d}', 'payload': {
        'index': index, 'chunk': base64.b64encode(chunk).decode('ascii')}}
        for index, chunk in enumerate((raw[:middle], raw[middle:]))]
    return {'key': key, 'payload': manifest}, parts


def research_library():
    import test_cloud_library as fixture

    library = fixture.sample()
    for event in library['event'].values():
        event['version'] = 'f' * 64
    library['research_item'] = {
        'verified': {
            'id': 'b' * 64, 'category': 'arbitration', 'period': '2026-09',
            'title': 'Проверенное судебное событие', 'original_text': 'Текст карточки дела',
            'summary': 'Краткое описание дела', 'status': 'verified',
            'competitor_code': 'a', 'source_name': 'Картотека',
            'source_url': 'https://example.org/case', 'published_at': '2026-09-12',
            'published_precision': 'day', 'version': 'e' * 64,
            'evidence': {'snapshot_sha256': 'd' * 64},
        },
        'candidate': {
            'id': 'c' * 64, 'category': 'media', 'period': '2026-09',
            'title': 'Кандидат из журнала', 'original_text': 'Упоминание конкурента',
            'status': 'candidate', 'published_at': '2026-09-13',
            'published_precision': 'day',
        },
    }
    return library


class ArbitrationTests(unittest.TestCase):
    def test_overview_counts_official_sources_once_and_research_stays_in_publications(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.screens import load_library

        library = research_library()
        app = cloud_fixture.CloudScreenTests().application()
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params.update(section='Обзор', period='2026-09')
        load_library.clear()
        member = {'email': 'reader@example.com', 'subject': 'test-subject',
                  'status': 'active', 'role': 'viewer'}
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('reader@example.com')), \
             patch('cloud.library.Repository.load', return_value=library), \
             patch('cloud.members.MemberStore.find', return_value=member), \
             patch('cloud.screens.event_card') as event_card, \
             patch('cloud.screens.section_heading') as section_heading, \
             patch('cloud.screens.research_card') as research_card, \
             patch('cloud.screens.competitor_bars') as competitor_bars:
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual([metric.value for metric in app.metric[:4]], ['2', '1', '0', '1'])
            self.assertIn(('Материалы периода', 'Всего 2'),
                          [call.args for call in section_heading.call_args_list])
            self.assertEqual([call.args[0]['id'] for call in event_card.call_args_list], [1, 2])
            self.assertEqual(competitor_bars.call_args.args[0][0]['id'], 1)
            self.assertEqual(len(competitor_bars.call_args.args[0]), 2)
            self.assertEqual([call.args[0]['title'] for call in research_card.call_args_list],
                             ['Проверенное судебное событие', 'Кандидат из журнала'])

            event_card.reset_mock()
            app.radio(key='nav_page').set_value('Публикации').run()
            kind_filter = app.selectbox(key='pub_kind')
            kind_filter.set_value('arbitration').run()
            self.assertFalse(app.exception)
            self.assertEqual([call.args[0]['title'] for call in event_card.call_args_list[-1:]],
                             ['Проверенное судебное событие'])
            event_card.reset_mock()
            app.selectbox(key='pub_kind').set_value('media').run()
            self.assertFalse(app.exception)
            self.assertFalse(event_card.call_args_list)
            self.assertTrue(any('Найдено материалов: 0' in caption.value for caption in app.caption))

    def test_individual_acceptance_uses_research_issue_version(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.draft_rules import create_payload, issues
        from cloud.screens import load_library

        library = research_library()
        payload = create_payload(library, '2026-09')
        draft = {**payload, 'id': 'd' * 32, 'period': '2026-09', 'revision': 1,
                 'updated_at': '2026-09-18T10:00:00+00:00', 'updated_by': 'editor@example.com'}
        library['research_item']['verified']['version'] = 'f' * 64
        library['research_item']['verified']['title'] = 'Обновлённая карточка дела'
        pending = issues(draft, library)
        self.assertEqual(len(pending), 1)
        research_id = pending[0]['event_id']
        app = cloud_fixture.CloudScreenTests().application({'editor_emails': ['editor@example.com']})
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params.update(section='Черновики', period='2026-09')
        load_library.clear()
        member = {'email': 'editor@example.com', 'subject': 'test-subject',
                  'status': 'active', 'role': 'editor'}
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('editor@example.com')), \
             patch('cloud.library.Repository.load', return_value=library), \
             patch('cloud.members.MemberStore.find', return_value=member), \
             patch('cloud.drafts.DraftService.open', return_value={
                 'draft': draft, 'issues': pending, 'library': library,
             }):
            app.run()
            app.radio(key='draft_group_' + app.session_state['draft_editor']['epoch']).set_value('arbitration').run()
            self.assertFalse(app.exception)
            checkbox = next(widget for widget in app.checkbox
                            if widget.label == 'Проверил изменения источника; сохранить мои заголовок и описание')
            checkbox.set_value(True).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['draft_editor']['accepted'], {research_id: 'f' * 64})

    def test_confirmed_research_events_reach_publications_and_editor_not_candidates(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.draft_rules import create_payload
        from cloud.screens import load_library

        library = fixture.sample()
        for event in library['event'].values():
            event['version'] = 'f' * 64
        for ident, kind, title, status in [
            (11, 'arbitration', 'Подтверждённое судебное событие', 'confirmed'),
            (12, 'media', 'Подтверждённое упоминание в журнале', 'confirmed'),
            (13, 'media', 'Кандидат из поиска', 'candidate'),
        ]:
            library['event'][str(ident)] = {
                **deepcopy(library['event']['1']), 'id': ident, 'kind': kind,
                'title': title, 'status': status, 'version': 'f' * 64,
            }
            library['period']['2026-09']['event_ids'].append(ident)
        library['research_item'] = {'13': {
            'id': 13, 'category': 'media', 'period': '2026-09',
            'title': 'Кандидат из поиска', 'status': 'candidate',
            'published_at': '2026-09-03', 'published_precision': 'day',
        }}
        payload = create_payload(library, '2026-09')
        self.assertEqual({item['event_id'] for item in payload['items']} & {11, 12, 13}, {11, 12})
        draft = {
            **payload, 'id': 'd' * 32, 'period': '2026-09', 'revision': 1,
            'updated_at': '2026-09-18T10:00:00+00:00', 'updated_by': 'editor@example.com',
        }
        app = cloud_fixture.CloudScreenTests().application({
            'editor_emails': ['editor@example.com'],
        })
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params.update(section='Публикации', period='2026-09')
        load_library.clear()
        member = {'email': 'editor@example.com', 'subject': 'test-subject',
                  'status': 'active', 'role': 'editor'}
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('editor@example.com')), \
             patch('cloud.library.Repository.load', return_value=library), \
             patch('cloud.members.MemberStore.find', return_value=member), \
             patch('cloud.screens.event_card') as event_card, \
             patch('cloud.drafts.DraftService.open', return_value={
                 'draft': draft, 'issues': [], 'library': library,
             }):
            app.run()
            self.assertFalse(app.exception)
            kind_filter = next(select for select in app.selectbox if select.label == 'Тип публикации')
            self.assertIn('Арбитражные дела', kind_filter.options)
            self.assertIn('Упоминания в СМИ', kind_filter.options)
            event_card.reset_mock()
            kind_filter.set_value('arbitration').run()
            self.assertFalse(app.exception)
            self.assertEqual([call.args[0]['title'] for call in event_card.call_args_list],
                             ['Подтверждённое судебное событие'])
            event_card.reset_mock()
            kind_filter = next(select for select in app.selectbox if select.label == 'Тип публикации')
            kind_filter.set_value('media').run()
            self.assertFalse(app.exception)
            self.assertEqual([call.args[0]['title'] for call in event_card.call_args_list],
                             ['Подтверждённое упоминание в журнале'])

            app.radio(key='nav_page').set_value('Черновики').run()
            self.assertFalse(app.exception)
            groups = app.radio(key='draft_group_' + app.session_state['draft_editor']['epoch'])
            self.assertIn('Арбитражные дела · 1', groups.options)
            self.assertIn('Упоминания в СМИ · 1', groups.options)
            self.assertFalse(any(item['event_id'] == 13 for item in app.session_state['draft_editor']['base']['items']))
            groups.set_value('arbitration').run()
            self.assertFalse(app.exception)
            self.assertEqual(next(field for field in app.text_input if field.label == 'Заголовок').value,
                             'Подтверждённое судебное событие')
            groups = app.radio(key='draft_group_' + app.session_state['draft_editor']['epoch'])
            groups.set_value('media').run()
            self.assertFalse(app.exception)
            self.assertEqual(next(field for field in app.text_input if field.label == 'Заголовок').value,
                             'Подтверждённое упоминание в журнале')

    def test_loads_latest_snapshot_only_from_active_import(self):
        manifest, parts = parts_fixture()
        with patch('cloud.arbitration.NeonHTTP') as client:
            client.return_value.query.side_effect = [[manifest], parts]
            self.assertEqual(load_snapshot({'cloud': {'database_url': 'unused'}}), SNAPSHOT)
            queries = client.return_value.query.call_args_list
            self.assertEqual(len(queries), 2)
            self.assertTrue(all("s.key='active'" in call.args[0] for call in queries))
            self.assertIn("r.kind='court_focus_snapshot'", queries[0].args[0])
            self.assertIn('ORDER BY r.key DESC LIMIT 1', queries[0].args[0])
            self.assertIn("r.kind='court_focus_part'", queries[1].args[0])
            self.assertEqual(queries[1].args[1], [manifest['key']])

    def test_missing_or_tampered_part_fails_closed(self):
        manifest, parts = parts_fixture()
        for corrupted in (parts[:1], [parts[0], {**parts[1], 'payload': {
            **parts[1]['payload'], 'chunk': base64.b64encode(b'altered').decode('ascii')}}],
                          [parts[1], parts[0]]):
            with self.subTest(corrupted=corrupted), patch('cloud.arbitration.NeonHTTP') as client:
                client.return_value.query.side_effect = [[manifest], corrupted]
                with self.assertRaises(ValueError):
                    load_snapshot({'cloud': {'database_url': 'unused'}})

    def test_denied_access_never_reads_snapshot(self):
        with patch('cloud.arbitration.current_access', return_value=Access()), \
             patch('cloud.arbitration.load_snapshot') as load, \
             patch('cloud.arbitration.st') as st:
            render_arbitration(lambda: {'cloud': {}}, lambda: {})
            load.assert_not_called()
            st.warning.assert_called_once()

    def test_old_arbitration_link_opens_overview(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.screens import load_library

        load_library.clear()
        app = cloud_fixture.CloudScreenTests().application()
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params['section'] = 'Арбитражные дела'
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('reader@example.com')), \
             patch('cloud.library.Repository.load', return_value=fixture.sample()), \
             patch('cloud.members.MemberStore.find', return_value={
                 'email': 'reader@example.com', 'subject': 'test-subject',
                 'status': 'active', 'role': 'viewer'}):
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.subheader[0].value, 'Обзор · Сентябрь 2026')
        self.assertEqual(app.radio[0].options, ['Обзор', 'Публикации', 'Конкуренты', 'Архив'])
        self.assertFalse(any(button.label == '← К рабочему пространству' for button in app.button))
        self.assertFalse(any(button.label == 'Арбитражные дела →' for button in app.button))

    def test_research_sections_keep_period_and_unknown_dates_separate(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.screens import load_library

        load_library.clear()
        app = cloud_fixture.CloudScreenTests().application()
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params.update(section='Обзор', period='2026-09')
        library = fixture.sample()
        library['research_run'] = {
            '1': {'id': 1, 'period': '2026-09', 'category': 'arbitration', 'status': 'partial',
                  'coverage': [{'source': 'Суд', 'status': 'error', 'detail': 'Недоступен'}],
                  'limitations': ['Пагинация не завершена']},
            '2': {'id': 2, 'period': '2026-09', 'category': 'media', 'status': 'success',
                  'coverage': [], 'limitations': []},
        }
        library['research_item'] = {
            '1': {'id': 1, 'category': 'arbitration', 'period': '2026-09',
                  'title': 'Дело сентября', 'status': 'candidate', 'source_name': 'Суд',
                  'source_url': 'https://example.org/case', 'published_at': '2026-09-12',
                  'published_precision': 'day'},
            '2': {'id': 2, 'category': 'media', 'period': None,
                  'title': 'Упоминание без даты', 'status': 'candidate',
                  'source_name': 'Журнал', 'source_url': 'https://example.org/magazine',
                  'published_precision': 'unknown'},
            '3': {'id': 3, 'category': 'arbitration', 'period': '2026-08',
                  'title': 'Дело августа', 'status': 'verified',
                  'source_name': 'Суд', 'source_url': 'https://example.org/old',
                  'published_at': '2026-08-12', 'published_precision': 'day'},
        }
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('reader@example.com')), \
             patch('cloud.library.Repository.load', return_value=library), \
             patch('cloud.members.MemberStore.find', return_value={
                 'email': 'reader@example.com', 'subject': 'test-subject',
                 'status': 'active', 'role': 'viewer'}):
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.subheader[0].value, 'Обзор · Сентябрь 2026')
        self.assertTrue(any('не полностью' in element.value for element in app.warning))
        self.assertTrue(any('Материалы без установленной даты · 1' in element.label for element in app.expander))
        from cloud.screens import latest_research_run, research_items
        self.assertEqual([item['title'] for item in research_items(library, '2026-09', 'arbitration')],
                         ['Дело сентября'])
        self.assertEqual([item['title'] for item in research_items(library)],
                         ['Упоминание без даты'])
        self.assertEqual(latest_research_run(library, '2026-09', 'media')['status'], 'success')

    def test_research_card_escapes_external_text_and_keeps_candidate_label(self):
        from cloud.presentation import research_card
        with patch('cloud.presentation.html') as render:
            research_card({'title': '<script>alert(1)</script>', 'summary': '<img src=x>',
                           'source_name': 'Журнал', 'status': 'candidate',
                           'published_precision': 'unknown',
                           'evidence': {'quote': '<b>Цитата</b>'}},
                          'https://example.org/news')
        markup = render.call_args.args[0]
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img', markup)
        self.assertIn('&lt;b&gt;Цитата&lt;/b&gt;', markup)
        self.assertIn('Требует проверки', markup)
        self.assertIn('Дата не установлена', markup)
        with patch('cloud.presentation.html') as render:
            research_card({'title': 'Подтверждённая цитата', 'status': 'verified',
                           'evidence': {'quote': 'Сохранённая цитата'}},
                          'https://example.org/news')
        self.assertIn('Цитата подтверждена снимком', render.call_args.args[0])

    def test_research_only_month_is_selectable_without_official_checks(self):
        import test_cloud_library as fixture
        import test_cloud_streamlit as cloud_fixture
        from cloud.screens import available_periods, load_library

        library = fixture.sample()
        library['research_run'] = {
            '1': {'id': 1, 'period': '2026-10', 'category': 'media', 'status': 'success',
                  'coverage': [], 'limitations': []},
            'bad': {'id': 2, 'period': '2026-99', 'category': 'media', 'status': 'success'},
        }
        library['research_item'] = {
            '1': {'id': 1, 'period': '2026-10', 'category': 'media', 'title': 'Октябрьская новость',
                  'status': 'candidate', 'source_name': 'Журнал',
                  'source_url': 'https://example.org/novosti', 'published_at': '2026-10-03',
                  'published_precision': 'day'},
        }
        self.assertEqual(available_periods(library, '2026-11'), ['2026-10', '2026-09'])

        load_library.clear()
        app = cloud_fixture.CloudScreenTests().application()
        app.secrets['cloud'] = fixture.CONFIG['cloud']
        app.query_params.update(section='Обзор', period='2026-10')
        with patch('streamlit.user', cloud_fixture.CloudScreenTests().user('reader@example.com')), \
             patch('cloud.library.Repository.load', return_value=library), \
             patch('cloud.members.MemberStore.find', return_value={
                 'email': 'reader@example.com', 'subject': 'test-subject',
                 'status': 'active', 'role': 'viewer'}):
            app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='nav_period').value, '2026-10')
        self.assertEqual(app.metric[0].value, '0')
        self.assertTrue(any('Проверки официальных источников за этот месяц не сохранены' in item.value
                            for item in app.info))

    def test_superseded_candidate_is_hidden_from_month_and_undated_cards(self):
        from cloud.screens import research_items

        library = {'research_item': {
            'old-month': {'id': 'old-month', 'category': 'media', 'period': '2026-09',
                          'title': 'Старый кандидат', 'superseded_by': 'verified'},
            'verified': {'id': 'verified', 'category': 'media', 'period': '2026-09',
                         'title': 'Подтверждённая запись', 'status': 'verified'},
            'old-undated': {'id': 'old-undated', 'category': 'media', 'period': None,
                            'title': 'Без даты, заменён', 'superseded_by': 'verified'},
            'undated': {'id': 'undated', 'category': 'media', 'period': None,
                        'title': 'Без даты, актуален', 'status': 'candidate'},
        }}
        self.assertEqual([item['id'] for item in research_items(library, '2026-09', 'media')],
                         ['verified'])
        self.assertEqual([item['id'] for item in research_items(library)], ['undated'])

    def test_unavailable_snapshot_does_not_expose_exception(self):
        with patch('cloud.arbitration.current_access', return_value=Access(role='viewer')), \
             patch('cloud.arbitration.load_snapshot', side_effect=RuntimeError('secret-host')), \
             patch('cloud.arbitration.st') as st:
            render_arbitration(lambda: {'cloud': {}}, lambda: {})
            self.assertNotIn('secret-host', st.error.call_args.args[0])

    def test_metric_summary_preserves_missing_and_stale_flags(self):
        metrics = {
            'petitionerAllCount3Year': {'data': 7, 'isActual': True},
            'defendantSupplySum': {'data': 125000, 'isActual': False},
            'petitionerLostCount1Year': {'data': None, 'isActual': None},
            'defendantAllCount12Month': {'isActual': True},
            'defendantAllCount3Year': {'data': 0, 'isActual': True},
        }
        self.assertIn('истец', metric_label('petitionerAllCount3Year'))
        self.assertIn('споры о поставке', metric_label('defendantSupplySum'))
        with patch('cloud.arbitration.st') as st:
            render_metrics(metrics)
            rows = st.dataframe.call_args.args[0]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1]['Актуальность'], 'Неактуален')
            self.assertEqual(rows[2]['Значение'], '0')
            self.assertFalse(any('isActual' in row['Значение'] for row in rows))
            self.assertTrue(any('Не передано' in call.args[0] for call in st.write.call_args_list))
