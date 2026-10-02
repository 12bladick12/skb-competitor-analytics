import tempfile
import unittest
from pathlib import Path
from price_monitor.storage import Store
from price_monitor.retired_documents import disable_document_jobs

ROOT=Path(__file__).resolve().parents[1]


def check_retirement(test,repo):
    repo.batch("INSERT INTO rules VALUES(1,'retired-test','megak','МЕГА-К','TEST','https://mega-k.com/products/test','','2026-09-30')")
    repo.batch("INSERT INTO runs(id,state,created_at) VALUES(1,'running','2026-09-30')")
    repo.batch("INSERT INTO jobs(id,run_id,rule_id,state) VALUES(1,1,1,'pending')")
    repo.batch("INSERT INTO passport_files(fingerprint,object_key,byte_size,page_count,kind,language,created_at) VALUES('saved','saved.pdf',12,1,'passport','ru','2026-09-30')")
    insert="INSERT INTO passport_jobs(id,rule_id,kind,payload_json,state,created_at,updated_at) VALUES(%(id)s,1,'download','{}',%(state)s,'2026-09-30','2026-09-30')"
    for state in ('pending','retry','processing','done'):repo.batch(insert,{'id':state,'state':state})
    disable_document_jobs(repo);disable_document_jobs(repo)
    states={r['id']:r['state'] for r in repo.batch('SELECT id,state FROM passport_jobs')}
    test.assertEqual(states,{'pending':'cancelled','retry':'cancelled','processing':'cancelled','done':'done'})
    # A still-running old release cannot enqueue or claim any document job.
    repo.batch(insert,{'id':'late-insert','state':'pending'})
    repo.batch("UPDATE passport_jobs SET state='processing',owner='old-instance' WHERE id='pending'")
    test.assertEqual(len(repo.batch('SELECT id FROM passport_jobs')),4)
    test.assertEqual(repo.batch("SELECT state FROM passport_jobs WHERE id='pending'")[0]['state'],'cancelled')
    test.assertEqual(repo.batch('SELECT state FROM jobs')[0]['state'],'pending')
    test.assertEqual(repo.batch('SELECT object_key FROM passport_files')[0]['object_key'],'saved.pdf')


class RetiredDocumentsTests(unittest.TestCase):
    def test_old_queue_fenced_without_deleting_history_or_price_jobs(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as folder:
            store=Store(Path(folder)/'db')
            try:check_retirement(self,store.catalog)
            finally:store.close()
