"""The server's one-minute read budget must survive the client deadline."""
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

import psycopg
from price_monitor.db_batches import batch
from price_monitor.db_connection import DeadlineConnection


class CatalogReadTimeoutTests(unittest.TestCase):
    def exercise(self, sql, *, timeout=None, fail=False):
        connection=DeadlineConnection.__new__(DeadlineConnection)
        connection.io_timeout=45.0
        cursor=Mock(description=None)
        cursor.nextset.return_value=False
        observed={}

        def execute(query, params):
            observed.update(query=query,deadline=connection.io_deadline)
            # Exercise the actual connection wait budget as well as SQL setup.
            connection.wait(iter(()))
            if fail:raise RuntimeError('test interrupted read')
            return cursor

        with patch('price_monitor.db_batches.connection_for',return_value=nullcontext(connection)), \
             patch('time.monotonic',return_value=100.0), \
             patch.object(DeadlineConnection,'execute',side_effect=execute), \
             patch.object(psycopg.Connection,'wait',return_value=None) as wait:
            if fail:
                with self.assertRaises(RuntimeError):batch({},sql,timeout=timeout)
            else:batch({},sql,timeout=timeout)
            observed['wait_budget']=wait.call_args.kwargs['timeout']
        self.assertIsNone(connection.io_deadline)
        self.assertEqual(connection.io_timeout,45.0)
        return observed

    def test_read_can_run_past_old_client_cutoff(self):
        result=self.exercise('SELECT 1')
        self.assertIn("statement_timeout='60s'",result['query'])
        self.assertTrue(result['query'].startswith('BEGIN READ ONLY;'))
        self.assertNotIn('pg_advisory_xact_lock',result['query'])
        self.assertEqual(result['deadline'],175.0)
        self.assertEqual(result['wait_budget'],75.0)

    def test_write_and_explicit_deadlines_remain_bounded(self):
        write=self.exercise('UPDATE jobs SET state=state WHERE id=0')
        self.assertIn("statement_timeout='30s'",write['query'])
        self.assertIn('pg_advisory_xact_lock',write['query'])
        self.assertEqual(write['wait_budget'],45.0)
        self.assertEqual(self.exercise('SELECT 1',timeout=3)['wait_budget'],3)

    def test_failed_read_restores_budget_before_connection_reuse(self):
        self.assertEqual(self.exercise('SELECT 1',fail=True)['wait_budget'],75.0)


if __name__=='__main__':unittest.main()
