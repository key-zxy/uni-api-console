import json
import sqlite3
import tempfile
import time
import unittest
from aiohttp.test_utils import TestClient, TestServer
from audit_console import Settings, create_app, parse_usage


class UsageParseTests(unittest.TestCase):
    def test_json_usage_with_cache(self):
        body = b'{"model":"m1","usage":{"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":80}}}'
        self.assertEqual(parse_usage(body, 'application/json'), (100, 20, 80, 'm1'))

    def test_stream_usage_last_chunk(self):
        sse = ('data: {"model":"m2","choices":[{"delta":{"content":"hi"}}]}\n\n'
               'data: {"model":"m2","usage":{"prompt_tokens":99448,"completion_tokens":193,'
               '"prompt_tokens_details":{"cached_tokens":94080}}}\n\ndata: [DONE]\n\n')
        self.assertEqual(parse_usage(sse.encode(), 'text/event-stream'), (99448, 193, 94080, 'm2'))

    def test_missing_usage_is_unknown(self):
        self.assertEqual(parse_usage(b'{"model":"m3"}', 'application/json'), (None, None, None, 'm3'))
        self.assertEqual(parse_usage(b'not-json', 'application/json'), (None, None, None, None))


class UsageEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = self.tmp.name + '/audit.sqlite3'
        settings = Settings('http://127.0.0.1:1', 'admin', db=self.db_path)
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer admin'}
        db = sqlite3.connect(self.db_path)
        now = time.time()
        db.executemany(
            'INSERT INTO requests(started,path,method,status,prompt_tokens,completion_tokens,cached_tokens,model) '
            'VALUES(?,?,?,?,?,?,?,?)',
            [(now - 60, '/v1/chat/completions', 'POST', 200, 100, 10, 40, 'model-a'),
             (now - 120, '/v1/chat/completions', 'POST', 200, 200, 20, 80, 'model-a'),
             (now - 3 * 86400, '/v1/chat/completions', 'POST', 200, 50, 5, 0, 'model-b')])
        db.commit()
        db.close()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_usage_requires_auth(self):
        self.assertEqual((await self.client.get('/_audit/api/usage')).status, 401)

    async def test_usage_aggregates_days_and_models(self):
        response = await self.client.get('/_audit/api/usage?days=7', headers=self.auth)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['totals'], {'requests': 3, 'prompt': 350, 'completion': 35, 'cached': 120})
        self.assertEqual(len(body['daily']), 2)
        totals = {m['model']: m for m in body['models']}
        self.assertEqual(totals['model-a']['requests'], 2)
        self.assertEqual(totals['model-a']['prompt'], 300)

    async def test_usage_rejects_bad_range(self):
        for days in ('0', '91', 'abc'):
            with self.subTest(days=days):
                self.assertEqual((await self.client.get('/_audit/api/usage?days=' + days, headers=self.auth)).status, 400)