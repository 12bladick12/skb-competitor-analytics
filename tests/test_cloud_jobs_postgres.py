"""Queue fencing and immutable publication on CI's disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest

from cloud.jobs import JobStore, LeaseLost
from cloud.draft_rules import snapshot
import test_cloud_drafts_postgres


@unittest.skipUnless(os.environ.get('SKB_TEST_POSTGRES_URL'),'isolated PostgreSQL required')
class JobPostgresTests(unittest.TestCase):
    def setUp(self):
        test_cloud_drafts_postgres.DraftPostgresTests.setUp(self)
        self.jobs=JobStore({},repository=self.repo)
        self.jobs.initialize()

    def queue(self, kind='collect', payload=None, key='1'):
        return self.jobs.enqueue(self.import_id,kind,'2026-09',payload or {},'editor@example.com',key)

    def test_concurrent_claim_has_one_winner_and_collection_clicks_deduplicate(self):
        first=self.queue()
        self.assertEqual(self.queue(key='2')['id'],first['id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(self.jobs.claim,['worker-a','worker-b']))
        self.assertEqual(sum(bool(c) for c in claims),1)
        winner=next(c for c in claims if c)
        self.jobs.pulse(winner,'Источник','https://example.com',3)
        self.assertEqual(self.jobs.list(self.import_id)[0]['completed'],3)

    def test_expired_job_is_interrupted_not_retried_and_stale_worker_cannot_publish(self):
        self.queue()
        job=self.jobs.claim('old')
        self.jobs.query("UPDATE skb_analytics.jobs SET lease_until=now()-interval '1 second' WHERE id=$1 RETURNING id",[job['id']],write=True)
        self.assertIsNone(self.jobs.claim('new'))
        self.assertEqual(self.jobs.list(self.import_id)[0]['status'],'interrupted')
        with self.assertRaises(LeaseLost):self.jobs.pulse(job)
        with self.assertRaises(Exception):self.jobs.publish(job,[{'kind':'report','key':'bad','payload':{}}],{})
        self.assertNotIn('bad',self.repo.load()['report'])

    def test_export_commit_checks_facts_and_preserves_previous_report(self):
        draft=self.store.read(self.import_id,'2026-09')
        saved=snapshot(draft,self.repo.load())
        self.queue('export',saved)
        job=self.jobs.claim('worker')
        self.jobs.publish(job,[{'kind':'report','key':'release','payload':{'revision':1,'snapshot':saved}}],{},result_id='release')
        self.assertEqual(self.jobs.list(self.import_id)[0]['status'],'success')
        self.queue('export',saved,key='next')
        second=self.jobs.claim('worker')
        self.jobs.publish(second,[{'kind':'report','key':'release','payload':{'revision':99}}],{})
        self.assertEqual(self.repo.load()['report']['release']['revision'],1)
        self.queue('export',saved,key='changed')
        changed=self.jobs.claim('worker')
        self.jobs.query("UPDATE skb_analytics.records SET payload=jsonb_set(payload,'{status}','\"rejected\"') WHERE kind='event' AND key='1' RETURNING key",write=True)
        with self.assertRaises(Exception):
            self.jobs.publish(changed,[{'kind':'report','key':'invalid','payload':{}}],{})
        self.assertNotIn('invalid',self.repo.load()['report'])

    def test_collection_commits_records_and_status_together_without_changing_drafts(self):
        original=self.store.read(self.import_id,'2026-09')
        self.queue()
        job=self.jobs.claim('worker')
        self.jobs.publish(job,[{'kind':'period','key':'2026-10','payload':{'event_ids':[],'checks':[]}}],{},status='partial')
        self.assertIn('2026-10',self.repo.load()['period'])
        self.assertEqual(self.store.read(self.import_id,'2026-09'),original)
        self.assertEqual(self.jobs.list(self.import_id)[0]['status'],'partial')

    def test_saved_revision_worker_export_download_and_second_release(self):
        import hashlib
        from io import BytesIO
        import json
        from pathlib import Path
        import tempfile
        from zipfile import ZipFile, ZIP_DEFLATED
        from cloud.library import read_asset
        from cloud.worker import execute
        class Drive:
            def __init__(self):self.files={};self.counter=0
            def allocate(self):
                self.counter+=1
                return 'test_private_'+str(self.counter)
            def ensure_folder(self,folder):pass
            def ensure_pack(self,ident,folder,content,digest):
                assert hashlib.sha256(content).hexdigest()==digest
                self.files[ident]=content
            def download(self,ident,digest):
                content=self.files[ident]
                assert hashlib.sha256(content).hexdigest()==digest
                return content
        drive=Drive()
        proof=b'<h1>Official evidence</h1><script>alert(1)</script>'
        digest=hashlib.sha256(proof).hexdigest()
        out=BytesIO()
        with ZipFile(out,'w',ZIP_DEFLATED) as archive:archive.writestr(digest,proof)
        packed=out.getvalue();pack=hashlib.sha256(packed).hexdigest()
        drive.files['test_initial_proof']=packed
        self.jobs.query("INSERT INTO skb_analytics.assets VALUES($1,$2,$3::jsonb) RETURNING id",
            [self.import_id,digest,json.dumps({'pack':pack,'drive_id':'test_initial_proof','bytes':len(proof)})],write=True)
        self.jobs.query("UPDATE skb_analytics.records SET payload=jsonb_set(payload,'{evidence_ids}',$1::jsonb) WHERE kind='event' RETURNING key",
            [json.dumps([digest])],write=True)
        self.jobs.query("INSERT INTO skb_analytics.records VALUES($1,'storage','drive',$2::jsonb) RETURNING key",
            [self.import_id,json.dumps({'folder_id':'test_folder_id'})],write=True)
        previous=None
        for number in (1,2):
            draft=self.store.read(self.import_id,'2026-09')
            draft['items'][0]['title']='Правка '+str(number)
            draft=self.store.commit(self.import_id,draft,draft['revision'],
                {'items':draft['items'],'conclusions':'Вывод '+str(number)},'editor@example.com')
            saved=snapshot(draft,self.repo.load(),self.store.asset_ids(self.import_id))
            self.queue('export',saved,key='release-'+str(number))
            job=self.jobs.claim('worker-'+str(number))
            with tempfile.TemporaryDirectory() as folder:execute(self.jobs,drive,job,Path(folder))
            report=self.repo.load()['report'][job['id']]
            word=read_asset(self.repo,drive,self.import_id,report['docx_id'])
            bundle=read_asset(self.repo,drive,self.import_id,report['bundle_id'])
            with ZipFile(BytesIO(bundle)) as archive:
                manifest=json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['items'][0]['title'],'Правка '+str(number))
                self.assertEqual(archive.read('report.docx'),word)
            if previous:
                self.assertEqual(read_asset(self.repo,drive,self.import_id,previous[0]),previous[1])
            previous=(report['docx_id'],word)
