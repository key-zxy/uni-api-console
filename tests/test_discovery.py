import unittest
from model_discovery import models_url, model_ids

class DiscoveryTests(unittest.TestCase):
    def test_urls(self):
        for suffix in ('chat/completions', 'responses', 'responses/compact'):
            self.assertEqual(models_url('https://example.com/api/v1/'+suffix), 'https://example.com/api/v1/models')
        self.assertEqual(models_url('https://example.com', 'https://catalog.example.com/exact'), 'https://catalog.example.com/exact')
        for url in ('ftp://example.com', 'https://example.com:0', 'https://example.com:65536', 'https://example.com:bad', 'https://@example.com', 'file:///etc/passwd', 'https://example.com/\\foo', 'https://user:pass@example.com', 'https://example.com?q=key', 'https://example.com/#x', 'https://example.com/\nfoo'):
            with self.assertRaises(ValueError): models_url(url)
    def test_internal_and_fake_ip_urls(self):
        for base in ('https://198.18.0.20/api/v1', 'https://10.0.0.1:8443/v1',
                     'http://localhost:8080/v1', 'http://provider.internal:8000/v1',
                     'https://[::1]:8443/v1'):
            self.assertEqual(models_url(base), base + '/models')
        self.assertEqual(models_url('https://openrouter.ai/api/v1'),
                         'https://openrouter.ai/api/v1/models')

    def test_openrouter_complete_catalog_and_pagination(self):
        items = [{'id': 'vendor/a'}, {'id': 'vendor/b'}]
        self.assertEqual(model_ids({'data': items, 'total_count': 2, 'links': {'next': None}}), ['vendor/a', 'vendor/b'])
        for extra in ({'total_count': 3, 'links': {'next': None}}, {'total_count': 2, 'links': {'next': 'https://example.com/page2'}}, {'total_count': 2, 'links': 'unexpected'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                model_ids({'data': items, **extra})

    def test_ids(self):
        self.assertEqual(model_ids({'data':[{'id':'paid'}, {'id':'paid'}, {'id':'vendor/model:free'}]}), ['paid','vendor/model:free'])
        for payload in ({}, {'data':[]}, {'data':[{'id':'bad\nvalue'}]}, {'data':[{'id':str(i)} for i in range(1001)]}):
            with self.assertRaises(ValueError): model_ids(payload)

from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import make_mocked_request
from audit_console import Settings, create_app
from model_discovery import ProviderResolver

class DiscoveryWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, payload, token='admin', origin=None):
        app = create_app(Settings('http://native-unconfigured:8000', 'admin'))
        headers = {'Authorization':'Bearer '+token}
        if origin: headers['Origin'] = origin
        request = make_mocked_request('POST', '/_audit/api/models/discover', headers=headers, app=app)
        request.json = AsyncMock(return_value=payload)
        match = await app.router.resolve(request)
        request._match_info = match
        handler = match.handler
        for middleware in reversed(app.middlewares):
            original = handler
            async def handler(r, middleware=middleware, original=original):
                return await middleware(r, original)
        return await handler(request)

    async def test_first_setup_auth_and_exact_destination(self):
        payload = dict(base_url='https://api.example.com/v1/responses', models_url='', api='test-secret')
        with patch('model_discovery.fetch_models', new=AsyncMock(return_value=['paid-model'])) as fetch:
            self.assertEqual((await self.call(payload, token='wrong')).status, 401)
            fetch.assert_not_awaited()
            self.assertEqual((await self.call(payload, origin='https://untrusted.example')).status, 403)
            fetch.assert_not_awaited()
            response = await self.call(payload)
            self.assertEqual(response.status, 200)
            fetch.assert_awaited_once_with('https://api.example.com/v1/models', 'test-secret')
            payload['models_url'] = 'http://provider.internal:8000/custom'
            self.assertEqual((await self.call(payload)).status, 200)
            fetch.assert_awaited_with('http://provider.internal:8000/custom', 'test-secret')

    async def test_reentry_errors_and_secret_redaction(self):
        payload = dict(base_url='https://api.example.com', models_url='', api='')
        with patch('model_discovery.fetch_models', new=AsyncMock(side_effect=ValueError('test-secret upstream-body'))) as fetch:
            self.assertEqual((await self.call(payload)).status, 400)
            fetch.assert_not_awaited()
            payload['api'] = 'test-secret'
            response = await self.call(payload)
            self.assertEqual(response.status, 400)
            self.assertNotIn('test-secret', response.text)
            self.assertNotIn('upstream-body', response.text)
            self.assertNotIn('仅允许公共 HTTPS', response.text)
            self.assertIn('HTTP/HTTPS', response.text)
        with patch('model_discovery.fetch_models', new=AsyncMock(return_value=['test-secret'])):
            self.assertEqual((await self.call(payload)).status, 400)

    async def test_resolver_permits_fake_ip_private_and_mixed_dns(self):
        import socket
        import asyncio
        loop = asyncio.get_running_loop()
        for addresses in (['198.18.0.20'], ['10.0.0.1'], ['127.0.0.1'], ['169.254.169.254'], ['::1'], ['8.8.8.8','10.0.0.1']):
            with patch.object(loop, 'getaddrinfo', new=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in addresses])):
                resolved = await ProviderResolver().resolve('openrouter.ai')
                self.assertEqual([entry['host'] for entry in resolved], addresses)

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_body_cap_and_options(self):
        from model_discovery import fetch_models
        class Content:
            def __init__(self, chunks): self.chunks = chunks
            async def iter_chunked(self, size):
                for chunk in self.chunks: yield chunk
        class Response:
            headers = {}
            status = 200
            content = Content([b'{"data":[{"id":"model"}]}'])
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        response = Response()
        class Session:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.connector = kwargs['connector']
                assert isinstance(self.connector._resolver, ProviderResolver)
                assert kwargs['trust_env'] is False
                assert kwargs['auto_decompress'] is False
                assert kwargs['timeout'].total == 12
            async def __aenter__(self): return self
            async def __aexit__(self, *args): await self.connector.close()
            def get(self, url, **kwargs):
                assert url in ('https://example.com/v1/models', 'http://provider.internal:8000/api/v1/models', 'https://openrouter.ai/api/v1/models')
                assert kwargs['allow_redirects'] is False
                assert kwargs['headers']['Authorization'] == 'Bearer test-secret'
                return response
        with patch('model_discovery.ClientSession', Session):
            self.assertEqual(await fetch_models('https://example.com/v1/models','test-secret'), ['model'])
            import json
            rows = [{'id': 'vendor/model-' + str(i), 'pricing': {'prompt': '0.001'},
                     'description': 'synthetic catalog ' * 80} for i in range(435)]
            response.content = Content([json.dumps({'data': rows}).encode()])
            self.assertEqual(await fetch_models(models_url('http://provider.internal:8000/api/v1'), 'test-secret'),
                             [row['id'] for row in rows])
            self.assertEqual(await fetch_models(models_url('https://openrouter.ai/api/v1'), 'test-secret'),
                             [row['id'] for row in rows])
            response.status = 302
            with self.assertRaises(ValueError): await fetch_models('https://example.com/v1/models','test-secret')
            response.status = 200
            response.content = Content([b'x' * 1048577])
            with self.assertRaises(ValueError): await fetch_models('https://example.com/v1/models','test-secret')
