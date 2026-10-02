import json
import unittest
from unittest.mock import MagicMock

from cloud.neon_http import NeonHTTP, NeonError, MigrationConnection
from cloud.library import Repository


CONFIG={'cloud':{'database_url':'postgresql://test:SECRET@ep-test-pooler.eu-central-1.aws.neon.tech/db?sslmode=require'}}


class NeonHTTPTests(unittest.TestCase):
    def client(self, payload, status=200):
        session=MagicMock()
        response=session.post.return_value.__enter__.return_value
        response.status_code=status
        response.iter_content.return_value=[json.dumps(payload).encode()]
        return NeonHTTP(CONFIG,session=session),session

    def test_tls_endpoint_and_separate_parameters_without_redirects(self):
        client,session=self.client({'rows':[{'value':1}]})
        self.assertEqual(client.query('SELECT $1::int AS value',[1]),[{'value':1}])
        call=session.post.call_args
        self.assertEqual(call.args[0],'https://ep-test.eu-central-1.aws.neon.tech/sql')
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(json.loads(call.kwargs['data']),{'query':'SELECT $1::int AS value','params':[1]})

    def test_errors_never_echo_server_or_credentials(self):
        client,_=self.client({'message':'SECRET DO_NOT_PRINT','code':'42P01'},400)
        with self.assertRaises(NeonError) as raised:
            client.query('SELECT 1')
        self.assertEqual(raised.exception.sqlstate,'42P01')
        self.assertNotIn('SECRET',str(raised.exception))
        self.assertNotIn('DO_NOT_PRINT',str(raised.exception))

    def test_http_load_preserves_decoded_objects_and_uninitialized_is_empty(self):
        repository=Repository(CONFIG)
        repository.http,_=self.client({'rows':[{'id':'id','manifest':{'counts':{}},'kind':'event','key':'1','payload':{'title':'Заголовок'}}]})
        self.assertEqual(repository.load()['event']['1'],{'title':'Заголовок'})
        repository.http,_=self.client({'code':'42P01'},400)
        self.assertIsNone(repository.load())

    def test_only_atomic_do_commands_can_use_migration_connection(self):
        client=MagicMock()
        connection=MigrationConnection(client)
        with self.assertRaises(ValueError):
            connection.execute('BEGIN; SELECT 1; COMMIT;')
        client.query.assert_not_called()

    def test_catalog_quotes_are_escaped_in_the_atomic_command(self):
        catalog={'manifest':{},'assets':{},'records':[{'kind':'event','key':'1','payload':{'text':"'; $migration$ DROP TABLE examples; --"}}]}
        repository=Repository(CONFIG)
        repository.http=MagicMock()
        repository.write_connect=lambda **_: MigrationConnection(repository.http)
        repository.activate(catalog,{})
        query=repository.http.query.call_args.args[0]
        self.assertTrue(query.startswith('DO '))
        self.assertIn("''",query)
