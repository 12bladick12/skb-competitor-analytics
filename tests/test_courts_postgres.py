"""Real transactional tests against a disposable localhost database only."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch
from cloud.jobs import JobStore
from courts.store import CourtStore
from courts.collector import record
from courts.service import CourtService
from courts.registry import entities
import test_cloud_drafts_postgres


@unittest.skipUnless(os.environ.get('SKB_TEST_POSTGRES_URL'),'isolated PostgreSQL required')
class CourtPostgresTests(unittest.TestCase):
    def setUp(self):
        test_cloud_drafts_postgres.DraftPostgresTests.setUp(self)
        self.courts=CourtStore({},repository=self.repo);self.courts.initialize()
        self.news=JobStore({},repository=self.repo)

    def court_job(self):
        self.courts.enqueue(self.import_id,'court_collect','2026-10',{},'scheduler','court')
        return self.courts.claim('worker')

    def test_normal_worker_does_not_claim_courts_and_court_worker_skips_news(self):
        self.courts.enqueue(self.import_id,'court_collect','2026-10',{},'scheduler','court')
        self.assertIsNone(self.news.claim('news'))
        job=self.courts.claim('court');self.assertEqual(job['kind'],'court_collect')
        self.courts.publish(job,[],{})
        self.news.enqueue(self.import_id,'collect','2026-10',{},'editor','news')
        self.assertIsNone(self.courts.claim('court'))
        self.assertEqual(self.news.claim('news')['kind'],'collect')

    def test_atomic_pair_and_competing_periods_reuse_one_court_job(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda p:self.courts.enqueue_pair(self.import_id,p,'editor',p),['2026-09','2026-10']))
        rows=self.courts.list(self.import_id)
        self.assertEqual(sum(r['kind']=='court_collect' for r in rows),1)
        self.assertEqual(sum(r['kind']=='collect' for r in rows),2)

    def test_checkpoint_is_fenced_preserves_versions_and_does_not_finish_job(self):
        job=self.court_job()
        self.courts.checkpoint(job,[record('court_case','a',{'number':'A','status':'open'})],{})
        self.courts.checkpoint(job,[record('court_case','a',{'number':'A','status':'closed'})],{})
        self.assertEqual(len(list(self.courts.all(self.import_id,'court_version'))),1)
        self.assertEqual(self.courts.list(self.import_id)[0]['status'],'running')
        self.courts.query("UPDATE skb_analytics.jobs SET lease_until=now()-interval '1 second' WHERE id=$1 RETURNING id",[job['id']],write=True)
        with self.assertRaises(Exception):self.courts.checkpoint(job,[record('court_case','a',{'number':'BAD'})],{})
        self.assertEqual(self.courts.get(self.import_id,'court_case','a')['number'],'A')

    def test_internal_records_do_not_leak_into_news_and_archive_filter_is_parameterized(self):
        job=self.court_job();entity=entities()[0]
        self.courts.checkpoint(job,[record('court_entity',entity['id'],entity),
            record('court_case','a',{'id':'a','number':'А76-27310/2021','entity_ids':[entity['id']],
                                   'participants':[{'inn':entity['inn'],'role':'plaintiff'}],'claim_amount':{'amount':'100','currency':'RUB'}}),
            record('court_news','court:fake',{'id':'court:fake'})],{})
        self.assertNotIn('court:fake',self.repo.load()['event'])
        service=CourtService(lambda:{},lambda:{})
        with patch.object(service,'context',return_value=(self.courts,None)):
            count,rows=service.archive(self.import_id,brand='teko',role='plaintiff',amount_from='50',currency='RUB')
            self.assertEqual(count,1);self.assertEqual(rows[0]['id'],'a')
            self.assertEqual(service.archive(self.import_id,query="'; DROP TABLE skb_analytics.jobs; --")[0],0)
        self.assertTrue(self.courts.list(self.import_id))


if __name__=='__main__':unittest.main()
