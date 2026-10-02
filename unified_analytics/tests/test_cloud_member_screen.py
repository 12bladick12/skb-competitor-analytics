import unittest
from copy import deepcopy
from unittest.mock import patch


class MemberScreenTests(unittest.TestCase):
    def test_admin_can_block_and_restore_without_removing_deny_record(self):
        from streamlit.testing.v1 import AppTest
        rows=[{'email':'owner@example.com','role':'admin','status':'active','protected':True,'revision':1},
              {'email':'reader@example.com','role':'viewer','status':'active','protected':False,'revision':1}]
        app=AppTest.from_string("""
from cloud.member_screen import render_members
render_members(lambda:{},lambda:{'email':'owner@example.com'})
""")
        def change(email,revision,role,status):
            row=next(r for r in rows if r['email']==email)
            assert row['revision']==revision
            row.update(role=role,status=status,revision=revision+1)
        with patch('cloud.member_screen.MemberService.list',side_effect=lambda:deepcopy(rows)), \
             patch('cloud.member_screen.MemberService.change',side_effect=change) as save:
            app.run()
            next(c for c in app.checkbox if c.label=='Заблокировать доступ').check().run()
            next(b for b in app.button if b.label=='Сохранить').click().run()
            self.assertEqual(rows[1]['status'],'blocked')
            self.assertFalse(any(c.label=='Заблокировать доступ' for c in app.checkbox))
            next(c for c in app.checkbox if c.label=='Показать заблокированных').check().run()
            next(c for c in app.checkbox if c.label=='Заблокировать доступ').uncheck().run()
            next(b for b in app.button if b.label=='Сохранить').click().run()
            self.assertEqual(rows[1]['status'],'active')
            self.assertEqual(save.call_count,2)
            self.assertEqual(len(rows),2)
            self.assertFalse(app.exception)
