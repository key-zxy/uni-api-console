import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import make_mocked_request
from audit_console import Settings, create_app


class GenericSetupUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'initial-config'
        self.settings = Settings('http://localhost:8000', 'ui-admin', management_key='uni-api-admin')
        self.settings.config_path = str(self.path)
        self.payload = {'api': 'provider-secret', 'provider': 'custom', 'base_url': 'https://api.example.com/v1/chat/completions', 'models': ['model-free', {'upstream-model':'public-model'}]}

    async def call(self, method='POST', payload=None, token='ui-admin'):
        app = create_app(self.settings)
        request = make_mocked_request(method, '/_audit/api/setup', headers={'Authorization': 'Bearer ' + token}, app=app)
        request.json = AsyncMock(return_value=self.payload if payload is None else payload)
        match = await app.router.resolve(request)
        request._match_info = match
        handler = match.handler
        for middleware in reversed(app.middlewares):
            original = handler
            async def handler(r, middleware=middleware, original=original):
                return await middleware(r, original)
        return await handler(request)

    async def test_success_and_no_clobber(self):
        response = await self.call()
        self.assertEqual(response.status, 201)
        result = json.loads(response.text)
        saved = json.loads(self.path.read_text())
        self.assertEqual(set(result), {'provider', 'client_api_key'})
        self.assertEqual(result['provider'], 'custom')
        self.assertEqual(saved['providers'], [{
            'provider': 'custom',
            'base_url': 'https://api.example.com/v1/chat/completions',
            'api': 'provider-secret',
            'model': ['model-free', {'upstream-model':'public-model'}],
        }])
        self.assertEqual(saved['api_keys'][0], {'api': 'uni-api-admin', 'role': 'admin', 'model': ['all']})
        self.assertEqual(saved['api_keys'][1], {'api': result['client_api_key'], 'model': ['custom/model-free', 'custom/public-model']})
        self.assertNotIn('openrouter-secret', response.text)
        self.assertNotIn('uni-api-admin', response.text)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        before = self.path.read_bytes()
        self.assertEqual((await self.call()).status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads((await self.call('GET')).text), {'configured': True})

    async def test_destination_policy_without_dns_before_bootstrap(self):
        import asyncio
        for url in ('https://openrouter.ai/api/v1', 'http://198.18.0.20:1234/v1',
                    'http://10.0.0.1:65535/v1', 'http://localhost:1/v1',
                    'https://[::1]:8443/v1', 'http://provider.internal:8080/v1',
                    'https://api.example.com:444/v1'):
            with self.subTest(url=url), patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                    new=AsyncMock(side_effect=AssertionError('validation must not resolve DNS'))) as dns:
                response = await self.call(payload=dict(self.payload, base_url=url,
                    models=['vendor/model:free', {'source:free':'alias:free'}]))
                self.assertEqual(response.status, 201)
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved['providers'][0]['base_url'], url)
                self.assertEqual(saved['api_keys'][1]['model'], ['custom/vendor/model:free', 'custom/alias:free'])
                dns.assert_not_awaited()
                self.path.unlink()

    async def test_incomplete_and_legacy_inputs_are_rejected(self):
        invalid = [dict(self.payload, base_url=url) for url in (
            'ftp://example.com', 'http://localhost:0', 'https://@example.com',
            'http://localhost:65536', 'https://example.com?x=y',
            'https://example.com/#fragment', 'https://user:pass@example.com')] + [
            dict(self.payload, models=['all']), dict(self.payload, models=[]),
            dict(self.payload, models=['x','x']), dict(self.payload, provider='admin'),
            dict(self.payload, api='uni-api-admin'),
            {'api': ''}, {'api': 'bad\nkey'}, {'api': 'x' * 16385},
            {'api': 'openrouter-secret', 'provider': 'other'},
            {'api': 'openrouter-secret', 'base_url': 'https://paid.example/v1'},
            {'api': 'openrouter-secret', 'model': 'paid/vendor-model'},
            {'api': 'openrouter-secret', 'model': 'vendor/model:free'},
            {'api': 'openrouter-secret', 'models': ['paid/model']},
            {'api': 'openrouter-secret', 'engine': 'gpt'},
            {'api': 'openrouter-secret', 'extra': True},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = await self.call(payload=payload)
                self.assertEqual(response.status, 400)
                self.assertFalse(self.path.exists())

    async def test_preconditions_auth_and_status_secret_redaction(self):
        self.assertEqual((await self.call(token='wrong')).status, 401)
        self.settings.management_key = ''
        self.assertEqual((await self.call()).status, 503)
        self.settings.management_key = 'uni-api-admin'
        self.settings.config_path = ''
        self.assertEqual((await self.call()).status, 503)
        self.settings.config_path = str(self.path.parent / 'missing' / 'config')
        self.assertEqual((await self.call()).status, 503)
        self.settings.config_path = str(self.path)
        self.path.write_text('{"provider_secret":"do-not-return"}')
        response = await self.call('GET')
        self.assertEqual(json.loads(response.text), {'configured': True})
        self.assertNotIn('do-not-return', response.text)

    async def test_atomic_publish_race_and_cleanup(self):
        def competing_link(source, destination):
            Path(destination).write_text('existing configuration')
            raise FileExistsError()
        with patch('os.link', side_effect=competing_link):
            self.assertEqual((await self.call()).status, 409)
        self.assertEqual(self.path.read_text(), 'existing configuration')
        self.assertEqual(list(self.path.parent.glob('.audit-setup-*')), [])

    async def test_dangling_symlink_is_not_overwritten(self):
        self.path.symlink_to(self.path.parent / 'missing')
        self.assertEqual((await self.call()).status, 409)
        self.assertTrue(self.path.is_symlink())

    def test_environment(self):
        settings = Settings.from_env({'AUDIT_UPSTREAM': 'http://localhost', 'AUDIT_ADMIN_TOKEN': 'ui-admin', 'AUDIT_CONFIG_PATH': '/config/api.yaml'})
        self.assertEqual(settings.config_path, '/config/api.yaml')


if __name__ == '__main__':
    unittest.main()
