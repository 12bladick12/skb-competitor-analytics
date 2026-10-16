import unittest
from unittest.mock import patch


class CourtUITests(unittest.TestCase):
    def test_admin_archive_handles_missing_data_and_is_all_time(self):
        from streamlit.testing.v1 import AppTest
        from courts.registry import entities
        script="""
from courts.ui import render
render({'id':'test'},lambda:{},lambda:{})
"""
        overview={'entities':entities(),'checks':[],'counts':{},'release':None}
        with patch('courts.service.CourtService.overview',return_value=overview),patch('courts.service.CourtService.archive',return_value=(0,[])) as archive:
            app=AppTest.from_string(script).run()
            self.assertFalse(app.exception)
            self.assertTrue(any('Внутренняя проверка' in w.value for w in app.warning))
            self.assertTrue(any('не означает отсутствие дел' in i.value for i in app.info))
            self.assertEqual(archive.call_args.kwargs['date_from'],'')
            next(s for s in app.selectbox if s.label=='Бренд').set_value('sensoren').run()
            self.assertFalse(app.exception);self.assertEqual(archive.call_args.kwargs['brand'],'sensoren')

    def test_unreleased_tab_hidden_from_reader_but_visible_to_enabled_admin(self):
        from streamlit.testing.v1 import AppTest
        script="""
from types import SimpleNamespace
from cloud.screens import render_library
library={'id':'test','event':{},'period':{'2026-09':{'event_ids':[],'checks':[]}},'competitor':{},'run':{},'report':{},'draft':{},'manifest':{'source_created_at':'2026-10-16T00:00:00+00:00'}}
render_library(library,SimpleNamespace(role=ROLE),lambda:{'courts':{'enabled':True}},lambda:{})
"""
        for role,visible in [('reader',False),('admin',True)]:
            app=AppTest.from_string(script.replace('ROLE',repr(role))).run()
            self.assertFalse(app.exception)
            options=app.sidebar.radio[0].options
            self.assertEqual('Судебные дела и исполнительные производства' in options,visible)


if __name__=='__main__':unittest.main()
