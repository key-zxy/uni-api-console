import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from audit_console import Settings, create_app

class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.value = {'provider':'demo', 'status':'complete', 'key_count':1, 'keys':[{'position':1,'status':'ok','source':'sub2api','kind':'key_quota','amount':12,'currency':'USD','unlimited':False,'windows':[{'window':'1d','remaining':5}],'actual_cost_usd':2,'actual_cost_samples':3,'actual_cost_source':'sub2api_usage','api_key':'hidden'}], 'actual_cost_usd':2,'actual_cost_samples':3,'actual_cost_source':'sub2api_usage'}
        self.code = 200
        self.seen = []
        async def upstream(r):
            self.seen.append((r.method,r.path,dict(r.query),r.headers.get('Authorization')))
            if isinstance(self.value,str):
                return web.Response(text=self.value,status=self.code)
            return web.json_response(self.value,status=self.code)
        app = web.Application()
        app.router.add_route('*','/{path:.*}',upstream)
        self.up = TestServer(app)
        await self.up.start_server()
        self.s = Settings(str(self.up.make_url('')).rstrip('/'),'admin',db=self.tmp.name+'/audit.sqlite3',management_key='management-secret')
        self.client = TestClient(TestServer(create_app(self.s)))
        await self.client.start_server()
        self.auth = {'Authorization':'Bearer admin'}
    async def asyncTearDown(self):
        await self.client.close()
        await self.up.close()
        self.tmp.cleanup()
    async def refresh(self):
        r = await self.client.post('/_audit/api/quotas/demo/refresh',json={'start_date':'2026-11-15','end_date':'2026-11-15'},headers=self.auth)
        self.assertEqual(r.status,200)
        return await r.json()
    async def test_policy_and_auth(self):
        self.assertEqual((await self.client.get('/_audit/api/quotas')).status,401)
        path = '/_audit/api/quotas/demo'
        good = {'daily_limit':12.5,'unit':'USD','timezone':'UTC','reset_time':'03:00'}
        for patch in ({'daily_limit':True},{'daily_limit':-1},{'timezone':'Fake/Zone'},{'reset_time':'24:00'},{'url':'https://evil'},{'unit':''}):
            r = await self.client.put(path,json={**good,**patch},headers=self.auth)
            self.assertEqual(r.status,400)
        self.assertEqual((await self.client.put(path,json=good,headers=self.auth)).status,200)
        rows = await (await self.client.get('/_audit/api/quotas',headers=self.auth)).json()
        self.assertEqual(rows[0]['policy']['daily_limit'],12.5)
    async def test_snapshot_request_and_secrets(self):
        result = await self.refresh()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['source'],'sub2api')
        self.assertTrue(result['balance_available'])
        self.assertEqual(result['balances'][0]['amount'],12)
        self.assertEqual(result['balances'][0]['kind'],'key_quota')
        self.assertEqual(result['daily_usage'],{'value':2,'unit':'USD','source':'sub2api_usage','samples':3})
        self.assertEqual(self.seen,[('GET','/v1/channel-balances',{'provider':'demo','start_date':'2026-11-15','end_date':'2026-11-15'},'Bearer management-secret')])
        self.assertNotIn('hidden',json.dumps(result))
        self.assertNotIn('api_key',json.dumps(result))
        db = sqlite3.connect(self.s.db)
        try:
            rows = db.execute('SELECT value FROM quota_snapshots').fetchall()
            self.assertEqual(len(rows),1)
            self.assertNotIn('hidden',rows[0][0])
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE quota_snapshots SET status='oops'")
        finally:
            db.close()

    async def test_quota_snapshots_follow_retention(self):
        await self.refresh()
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(self.s.db)) as db:
            db.executemany('INSERT INTO quota_snapshots(provider,start_date,end_date,checked_at,status,source,value) VALUES(?,?,?,?,?,?,?)', [
                ('expired','2020-01-01','2020-01-01',(now-timedelta(days=30)).isoformat(),'ok','sub2api','{}'),
                ('recent','2026-11-15','2026-11-15',now.isoformat(),'ok','sub2api','{}'),
            ])
            db.commit()
        response = await self.client.post('/_audit/api/cleanup',headers=self.auth)
        self.assertEqual(response.status,200)
        self.assertEqual(await response.json(),{'deleted':1})
        with closing(sqlite3.connect(self.s.db)) as db:
            remaining = db.execute('SELECT provider FROM quota_snapshots ORDER BY provider').fetchall()
        self.assertEqual(remaining,[('demo',),('recent',)])
    async def test_failures(self):
        for value, code, status in [({'status':'unsupported'},200,'unsupported'), ({'error':'management-secret'},403,'http_error'), ('not json management-secret',200,'malformed_json'), ('x'*1048577,200,'body_too_large')]:
            self.value, self.code = value, code
            result = await self.refresh()
            self.assertEqual(result['status'],status)
            self.assertFalse(result['balance_available'])
            self.assertNotIn('management-secret',json.dumps(result))
        self.s.management_key = ''
        self.assertEqual((await self.refresh())['status'],'missing_management_key')

    async def test_validation_and_method_auth(self):
        for method, path in [('put','/_audit/api/quotas/demo'),('post','/_audit/api/quotas/demo/refresh')]:
            self.assertEqual((await getattr(self.client,method)(path,json={})).status,401)
        self.assertEqual((await self.client.post('/_audit/api/quotas/demo/refresh',json={'start_date':'yesterday','end_date':'2026-11-15'},headers=self.auth)).status,400)

class QuotaCoreTests(unittest.TestCase):
    def test_policy_validation(self):
        from quotas import validate_policy
        good = {'daily_limit':12.5,'unit':'USD','timezone':'UTC','reset_time':'03:00'}
        self.assertEqual(validate_policy(good),good)
        for patch in ({'daily_limit':True},{'daily_limit':float('nan')},{'daily_limit':-1},{'timezone':'Fake/Zone'},{'reset_time':'24:00'},{'url':'https://evil'},{'unit':''}, {'reset_time':''}, {'timezone':None}):
            with self.assertRaises(ValueError):
                validate_policy({**good,**patch})

    def test_sanitized_immutable_storage(self):
        from quotas import initialize, save_snapshot
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        initialize(db)
        result = save_snapshot(db,'demo','2026-11-01','2026-11-15','ok','sub2api',
                               {'balance':{'amount':12,'actual_cost':2},'api_key':'hidden','unexpected-secret-value':'management-secret'}, secrets=('management-secret',))
        self.assertEqual(result['value']['balance']['actual_cost'],2)
        self.assertNotIn('hidden',json.dumps(result))
        self.assertNotIn('management-secret',json.dumps(result))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE quota_snapshots SET status='oops'")
        db.execute('DELETE FROM quota_snapshots')
        self.assertEqual(db.execute('SELECT count(*) FROM quota_snapshots').fetchone()[0],0)

    def test_reset_sources(self):
        from quotas import next_reset
        now = datetime(2026,11,15,4,tzinfo=timezone.utc)
        policy = {'timezone':'UTC','reset_time':'03:00'}
        self.assertEqual(next_reset({},None,now),{'at':None,'source':None})
        self.assertEqual(next_reset({'windows':[{'window':'1d','Retry-After':12}]},policy,now),
                         {'at':'2026-11-16T03:00:00+00:00','source':'configured_policy'})
        self.assertEqual(next_reset({'reset_at':'2026-11-15T09:00:00Z'},policy,now)['source'],'provider_reported')
        self.assertEqual(next_reset({'reset_at':'tomorrow'},None,now)['at'],None)

    def test_incomplete_policy(self):
        from quotas import validate_policy
        for value in ({'reset_time':''}, {'reset_time':'03:00'}, {'timezone':None}):
            with self.assertRaises(ValueError): validate_policy(value)

    def test_sanitize_usage(self):
        from quotas import sanitize
        value = {'prompt_tokens':42,'completion_tokens':7,'nested':[{'Authorization':'Bearer hidden','access_token':'hidden','cookie':'hidden','secret':'hidden','password':'hidden','refresh_token':'hidden','credential':'hidden','api_key':'hidden','note':'management-secret'}]}
        self.assertEqual(sanitize(value, secrets=('management-secret',)), {'prompt_tokens':42,'completion_tokens':7,'nested':[{}]})

class QuotaSocketFreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_and_persistence_without_sockets(self):
        from unittest.mock import AsyncMock, patch
        from aiohttp.test_utils import make_mocked_request
        class Content:
            async def iter_chunked(self, size):
                yield json.dumps({'status':'ok','source':'sub2api','balance':{'amount':12,'prompt_tokens':4},'api_key':'hidden','copy':'hidden'}).encode()
        class Response:
            status = 200
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        with tempfile.TemporaryDirectory(dir='.') as tmp:
            settings = Settings('https://example.invalid/base','admin',db=tmp+'/audit.sqlite3',management_key='management-secret')
            with patch('audit_console.ClientSession') as session:
                session.return_value.get.return_value = Response()
                session.return_value.close = AsyncMock()
                app = create_app(settings)
                lifecycle = app.cleanup_ctx[0](app)
                await anext(lifecycle)
                try:
                    request = make_mocked_request('POST','/_audit/api/quotas/demo%20name/refresh',app=app)
                    match = await app.router.resolve(request)
                    request._match_info = match
                    request.json = AsyncMock(return_value={'start_date':'2026-11-01','end_date':'2026-11-15'})
                    denied = await app.middlewares[0](request, match.handler)
                    self.assertEqual(denied.status,401)
                    result = json.loads((await match.handler(request)).text)
                    self.assertEqual(result['status'],'ok')
                    self.assertTrue(result['balance_available'])
                    self.assertIsNone(result['daily_usage']['source'])
                    self.assertNotIn('hidden',json.dumps(result))
                    args, kwargs = session.return_value.get.call_args
                    self.assertEqual(args[0],'https://example.invalid/base/v1/channel-balances')
                    self.assertEqual(kwargs['params']['provider'],'demo name')
                    self.assertEqual(kwargs['headers']['Authorization'],'Bearer management-secret')
                    self.assertFalse(kwargs['allow_redirects'])
                    for payload, code, expected in [(b'{"status":"unsupported"}',200,'unsupported'), (b'private error',403,'http_error'), (b'bad json',200,'malformed_json'), (b'x'*1048577,200,'body_too_large'), (b'{}',200,'unknown')]:
                        class FailureContent:
                            async def iter_chunked(self, size): yield payload
                        session.return_value.get.return_value.content = FailureContent()
                        session.return_value.get.return_value.status = code
                        failure = json.loads((await match.handler(request)).text)
                        self.assertEqual(failure['status'],expected)
                        self.assertFalse(failure['balance_available'])
                    policy_request = make_mocked_request('PUT','/_audit/api/quotas/demo%20name',app=app)
                    policy_match = await app.router.resolve(policy_request)
                    policy_request._match_info = policy_match
                    policy_request.json = AsyncMock(return_value={'daily_limit':12,'unit':'USD','timezone':'UTC','reset_time':'03:00'})
                    self.assertEqual((await policy_match.handler(policy_request)).status,200)
                    list_request = make_mocked_request('GET','/_audit/api/quotas',app=app)
                    list_match = await app.router.resolve(list_request)
                    listed = json.loads((await list_match.handler(list_request)).text)
                    self.assertEqual(listed[0]['policy']['daily_limit'],12)
                    self.assertEqual(listed[0]['next_reset']['source'],'configured_policy')
                    settings.management_key = ''
                    self.assertEqual(json.loads((await match.handler(request)).text)['status'],'missing_management_key')
                    with closing(sqlite3.connect(settings.db)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM quota_snapshots').fetchone()[0],7)
                        self.assertNotIn('hidden',db.execute('SELECT value FROM quota_snapshots').fetchone()[0])
                finally:
                    with self.assertRaises(StopAsyncIteration): await anext(lifecycle)
