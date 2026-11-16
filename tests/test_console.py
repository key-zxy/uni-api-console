import unittest
from audit_console import Settings, create_app

class SettingsTests(unittest.TestCase):
    def test_required_and_local_defaults(self):
        for env in ({}, {'AUDIT_UPSTREAM':'http://localhost'}):
            with self.assertRaises(ValueError):
                Settings.from_env(env)
        s = Settings.from_env({'AUDIT_UPSTREAM':'http://localhost', 'AUDIT_ADMIN_TOKEN':'secret'})
        self.assertEqual(s.host, '127.0.0.1')
        self.assertNotIn('secret', repr(s))
        with self.assertRaises(ValueError):
            Settings('http://localhost','secret',host='0.0.0.0')
    def test_container_bind_requires_explicit_opt_in(self):
        base = {'AUDIT_UPSTREAM':'http://uni-api:8000','AUDIT_ADMIN_TOKEN':'secret','AUDIT_HOST':'0.0.0.0'}
        with self.assertRaises(ValueError):
            Settings.from_env(base)
        with self.assertRaises(ValueError):
            Settings.from_env({**base,'AUDIT_ALLOW_NONLOOPBACK_BIND':'1','AUDIT_HOST':'10.20.30.40'})
        settings = Settings.from_env({**base,'AUDIT_ALLOW_NONLOOPBACK_BIND':'1'})
        self.assertEqual(settings.host,'0.0.0.0')

import asyncio
import json
import sqlite3
from contextlib import contextmanager

@contextmanager
def managed_db(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()

import tempfile
import time
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

class RootRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_serves_console_without_affecting_api(self):
        from aiohttp.test_utils import make_mocked_request
        app = create_app(Settings('http://127.0.0.1:1', 'test-token', db=':memory:'))
        app.freeze()
        request = make_mocked_request('GET', '/', app=app)
        match = await app.router.resolve(request)
        self.assertNotEqual(match.handler.__name__, 'proxy')
        response = await match.handler(request)
        self.assertEqual(response.status, 200)
        self.assertIn(b'<!doctype html>', response.body.lower())
        other = await app.router.resolve(make_mocked_request('POST', '/v1/chat/completions', app=app))
        self.assertEqual(other.handler.__name__, 'proxy')

class FaviconRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_favicon_bypasses_proxy_and_audit(self):
        from aiohttp.test_utils import make_mocked_request
        app = create_app(Settings('http://127.0.0.1:1', 'test-token', db=':memory:'))
        app.freeze()
        for path in ('/favicon.svg', '/favicon.ico'):
            for method in ('GET', 'HEAD'):
                request = make_mocked_request(method, path, app=app)
                match = await app.router.resolve(request)
                self.assertNotEqual(match.handler.__name__, 'proxy')
                # No lifecycle/session/database is initialized: touching proxy or
                # audit state would fail, so this also proves local independence.
                response = await match.handler(request)
                self.assertEqual(response.status, 200)
                self.assertIsInstance(response, web.Response)
                self.assertIn(b'<svg', response.body)
                self.assertIn(b'double-fork-tree', response.body)
        other = await app.router.resolve(make_mocked_request('GET', '/favicon-other.ico', app=app))
        self.assertEqual(other.handler.__name__, 'proxy')

class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []
        async def upstream(r):
            body = await r.read()
            self.seen.append((r.method, r.raw_path, body, dict(r.headers)))
            if r.path == '/sse':
                out = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
                await out.prepare(r)
                for chunk in (b'data: {"x":', b'1}\n\n', b'data: [DONE]\n\n'):
                    await out.write(chunk)
                    await asyncio.sleep(.01)
                return out
            if r.path == '/noheaders':
                r.transport.close()
                return web.Response()
            if r.path == '/gzip-response':
                import gzip
                return web.Response(body=gzip.compress(b'compressed reply',mtime=0),headers={'Content-Encoding':'gzip','Connection':'x-remove','x-remove':'hidden','Set-Cookie':'session=private'})
            if r.path == '/broken':
                out = web.StreamResponse(headers={'Content-Length':'100'})
                await out.prepare(r)
                await out.write(b'partial')
                r.transport.close()
                return out
            return web.Response(body=body or b'ok', status=201, content_type='application/json')
        up = web.Application(handler_args={'auto_decompress':False})
        up.router.add_route('*','/{path:.*}',upstream)
        self.up = TestServer(up)
        await self.up.start_server()
        self.s = Settings(str(self.up.make_url('')).rstrip('/'), 'admin-secret', db=self.tmp.name+'/audit.db', response_cap=32)
        self.client = TestClient(TestServer(create_app(self.s)))
        await self.client.start_server()
        self.auth = {'Authorization':'Bearer admin-secret'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.up.close()
        self.tmp.cleanup()

    async def rows(self):
        r = await self.client.get('/_audit/api/requests', headers=self.auth)
        self.assertEqual(r.status,200)
        return await r.json()

    async def test_favicon_is_local_and_not_audited(self):
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                r = await self.client.request(method, '/favicon.ico')
                self.assertEqual(r.status, 200)
                if method == 'GET':
                    self.assertIn('<svg', await r.text())
                else:
                    self.assertEqual(await r.read(), b'')
        self.assertEqual(self.seen, [])
        self.assertEqual(await self.rows(), [])
        # Other paths still reach the proxy and produce audit records.
        r = await self.client.get('/favicon-other.ico')
        self.assertEqual(r.status, 201)
        await r.read()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_auth_health_and_redaction(self):
        self.assertEqual((await self.client.get('/health')).status,200)
        self.assertEqual((await self.client.get('/_audit/api/requests')).status,401)
        self.assertEqual((await self.client.get('/_audit/api/requests', headers={'Authorization':'Bearer wrong'})).status,401)
        r = await self.client.post('/v1/chat/completions?x=1&api_key=raw-secret', data=b'{"model":"demo"}', headers={'Authorization':'Bearer raw-key','Cookie':'raw-cookie','Content-Type':'application/json','Connection':'x-secret','x-secret':'hidden'})
        self.assertEqual(r.status,201)
        self.assertEqual(await r.read(), b'{"model":"demo"}')
        rows = await self.rows()
        self.assertEqual(rows[0]['model'],'demo')
        self.assertTrue(rows[0]['complete'])
        self.assertNotIn('raw-', json.dumps(rows))
        self.assertNotIn('x-secret', {k.lower():v for k,v in self.seen[0][3].items()})
        self.assertEqual(self.seen[0][1], '/v1/chat/completions?x=1&api_key=raw-secret')
        detail = await (await self.client.get('/_audit/api/requests/1',headers=self.auth)).json()
        self.assertEqual(detail['request_body'],'{"model":"demo"}')
        self.assertEqual(detail['response_body'],'{"model":"demo"}')

    async def test_sse_and_truncation(self):
        r = await self.client.get('/sse')
        self.assertEqual(await r.read(), b'data: {"x":1}\n\ndata: [DONE]\n\n')
        self.assertTrue((await self.rows())[0]['complete'])
        r = await self.client.post('/large',data=b'x'*100)
        self.assertEqual(await r.read(),b'x'*100)
        row = (await self.rows())[0]
        self.assertFalse(row['complete'])
        self.assertTrue(row['response_truncated'])

    async def test_error_is_incomplete(self):
        r = await self.client.get('/broken')
        try:
            await r.read()
        except Exception:
            pass
        row = (await self.rows())[0]
        self.assertFalse(row['complete'])
        self.assertEqual(row['error'],'upstream_error')

    async def test_request_cap(self):
        self.s.request_cap = 4
        r = await self.client.post('/large',data=b'12345')
        self.assertEqual(r.status,413)
        self.assertEqual(len(self.seen),0)
        row = (await self.rows())[0]
        self.assertTrue(row['request_truncated'])
        self.assertFalse(row['complete'])

    async def test_retention_search_delete_and_readonly(self):
        await (await self.client.post('/hello',data=b'{}')).read()
        r = await self.client.get('/_audit/api/requests?search=missing',headers=self.auth)
        self.assertEqual(await r.json(),[])
        r = await self.client.put('/_audit/api/settings',json={'retention_days':1},headers=self.auth)
        self.assertEqual(r.status,200)
        with managed_db(self.s.db) as db:
            db.execute('UPDATE requests SET started=?',(time.time()-172800,))
        r = await self.client.post('/_audit/api/cleanup',headers=self.auth)
        self.assertEqual((await r.json())['deleted'],1)
        self.assertEqual(await self.rows(),[])
        await (await self.client.get('/hello')).read()
        self.assertEqual((await self.client.delete('/_audit/api/requests/1',headers=self.auth)).status,200)
        self.assertEqual((await self.client.post('/_audit/api/config',json={},headers=self.auth)).status,405)

    async def test_ui_and_compressed_request_bytes(self):
        import gzip
        r = await self.client.get('/_audit')
        self.assertEqual(r.status,200)
        self.assertIn('uni-api',await r.text())
        self.assertIn('data-brand="double-fork-tree"',await r.text())
        raw = gzip.compress(b'{"model":"compressed"}')
        r = await self.client.post('/gzip', data=raw, headers={'Content-Encoding':'gzip'})
        self.assertEqual(await r.read(), raw)
        self.assertEqual(self.seen[-1][2],raw)

    async def test_live_sse_disconnect(self):
        ready = asyncio.Event()
        release = asyncio.Event()
        async def live(r):
            response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(r)
            await response.write(b'data: first\n\n')
            ready.set()
            await release.wait()
            with self.assertRaises(ConnectionError):
                for _ in range(10):
                    await response.write(b'data: last\n\n')
                    await asyncio.sleep(.01)
            return response
        up = web.Application()
        up.router.add_get('/live',live)
        server = TestServer(up)
        await server.start_server()
        original = self.s.upstream
        self.s.upstream = str(server.make_url('')).rstrip('/')
        try:
            r = await self.client.get('/live')
            await ready.wait()
            first = await asyncio.wait_for(r.content.read(13),1)
            self.assertEqual(first,b'data: first\n\n')
            r.close()
            release.set()
            for _ in range(100):
                rows = await self.rows()
                if rows:
                    break
                await asyncio.sleep(.01)
            self.assertFalse(rows[0]['complete'])
            self.assertEqual(rows[0]['error'],'client_disconnect')
        finally:
            release.set()
            await server.close()
            self.s.upstream = original

    async def test_management_is_bounded_redacted_and_no_writes(self):
        async def management(r):
            self.assertEqual(r.headers['Authorization'],'Bearer management-secret')
            return web.json_response({'api_key':'do-not-echo','dynamic-secret-key':{'nested':'do-not-echo'}})
        up = web.Application()
        up.router.add_get('/schema',management)
        up.router.add_get('/snapshot',management)
        server = TestServer(up)
        await server.start_server()
        self.s.upstream = str(server.make_url('')).rstrip('/')
        self.s.schema_path = '/schema'
        self.s.snapshot_path = '/snapshot'
        self.s.management_key = 'management-secret'
        self.s.response_cap = 4096
        try:
            r = await self.client.get('/_audit/api/config',headers=self.auth)
            body = await r.text()
            self.assertNotIn('do-not-echo',body)
            self.assertNotIn('dynamic-secret-key',body)
            self.assertNotIn('management-secret',body)
            self.assertIn('object_fields',body)
        finally:
            await server.close()

    async def test_startup_and_periodic_cleanup_persist_settings(self):
        await (await self.client.get('/old')).read()
        await self.client.put('/_audit/api/settings',json={'retention_days':2},headers=self.auth)
        await self.client.close()
        with managed_db(self.s.db) as db:
            db.execute('UPDATE requests SET started=?',(time.time()-3*86400,))
        self.s.cleanup_interval = .02
        self.client = TestClient(TestServer(create_app(self.s)))
        await self.client.start_server()
        self.assertEqual(await self.rows(),[])
        r = await self.client.get('/_audit/api/settings',headers=self.auth)
        self.assertEqual((await r.json())['retention_days'],2)
        await (await self.client.get('/old')).read()
        with managed_db(self.s.db) as db:
            db.execute('UPDATE requests SET started=?',(time.time()-3*86400,))
        await asyncio.sleep(.06)
        self.assertEqual(await self.rows(),[])

    async def test_response_encoding_headers_and_transport_failure(self):
        import gzip
        r = await self.client.get('/gzip-response', auto_decompress=False)
        raw = gzip.compress(b'compressed reply',mtime=0)
        self.assertEqual(await r.read(),raw)
        self.assertEqual(r.headers['Content-Encoding'],'gzip')
        self.assertNotIn('x-remove',r.headers)
        self.assertIn('Set-Cookie',r.headers)
        row = (await self.rows())[0]
        self.assertTrue(row['response_truncated'])
        r = await self.client.post('/noheaders',data=b'{}')
        self.assertEqual(r.status,502)
        await r.read()
        row = (await self.rows())[0]
        self.assertFalse(row['complete'])
        self.assertEqual(row['error'],'upstream_error')

    async def test_filter_and_invalid_retention(self):
        await (await self.client.post('/v1/test',json={'model':'hello'})).read()
        r = await self.client.get('/_audit/api/requests?method=POST&status=201&model=hello',headers=self.auth)
        self.assertEqual(len(await r.json()),1)
        for days in (0,True,'7',3651):
            r = await self.client.put('/_audit/api/settings',json={'retention_days':days},headers=self.auth)
            self.assertEqual(r.status,400)
        r = await self.client.delete('/_audit/api/requests/1',headers=self.auth)
        self.assertEqual(r.status,200)
        self.assertEqual(await self.rows(),[])
