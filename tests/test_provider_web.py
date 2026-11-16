import json
import stat
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from audit_console import Settings, create_app


class GenericBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='.')
        self.config_path = Path(self.tmp.name) / 'config' / 'api.yaml'
        self.config_path.parent.mkdir()
        self.calls = []

        async def upstream(request):
            self.calls.append((request.method, request.path, dict(request.headers)))
            return web.json_response({'api_config': {'providers': [], 'api_keys': []}})

        upstream_app = web.Application()
        upstream_app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        settings = Settings(
            str(self.upstream.make_url('').with_path('').with_query('')).rstrip('/'),
            'ui-admin-token',
            db=str(Path(self.tmp.name) / 'audit.sqlite3'),
            management_key='uni-api-admin-secret',
        )
        setattr(settings, 'config_path', str(self.config_path))
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()
        self.auth = {'Authorization': 'Bearer ui-admin-token'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.tmp.cleanup()

    async def test_bootstrap_accepts_generic_provider_and_reveals_only_client_key(self):
        self.assertEqual((await self.client.get('/_audit/api/setup')).status, 401)
        status_response = await self.client.get('/_audit/api/setup', headers=self.auth)
        self.assertEqual(status_response.status, 200)
        self.assertEqual(await status_response.json(), {'configured': False})

        response = await self.client.post(
            '/_audit/api/setup',
            json={
                'api': 'upstream-secret-for-test',
                'provider': 'my-vendor',
                'base_url': 'https://vendor.example/v1/chat/completions',
                'models': ['vendor/model-a', 'vendor/model-b:free'],
            },
            headers=self.auth,
        )
        self.assertEqual(response.status, 201)
        result = await response.json()
        client_key = result['client_api_key']
        self.assertTrue(client_key)
        self.assertEqual(result['provider'], 'my-vendor')
        self.assertNotIn('upstream-secret-for-test', json.dumps(result))
        self.assertNotIn('uni-api-admin-secret', json.dumps(result))
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved['providers'], [{
            'provider': 'my-vendor',
            'base_url': 'https://vendor.example/v1/chat/completions',
            'api': 'upstream-secret-for-test',
            'model': ['vendor/model-a', 'vendor/model-b:free'],
        }])
        self.assertEqual(saved['api_keys'][0], {'api': 'uni-api-admin-secret', 'role': 'admin', 'model': ['all']})
        self.assertEqual(saved['api_keys'][1], {'api': client_key, 'model': ['my-vendor/vendor/model-a', 'my-vendor/vendor/model-b:free']})

        status_response = await self.client.get('/_audit/api/setup', headers=self.auth)
        self.assertEqual(await status_response.json(), {'configured': True})
        duplicate = await self.client.post('/_audit/api/setup', json={'api': 'another-key'}, headers=self.auth)
        self.assertEqual(duplicate.status, 409)
        self.assertEqual(self.calls, [])

    async def test_setup_rejects_invalid_or_partial_fields(self):
        invalid = [
            {'api': ''},
            {'api': 'bad\nkey'},
            {'api': 'secret', 'provider': 'x'},
            {'api': 'secret', 'provider': 'x', 'base_url': 'https://example.com/v1', 'models': []},
            {'api': 'secret', 'provider': 'x', 'base_url': 'https://example.com/v1', 'models': ['all']},
            {'api': 'secret', 'provider': 'x', 'base_url': 'https://example.com/v1', 'models': ['bad model']},
            {'api': 'secret', 'provider': 'x', 'base_url': 'https://example.com/v1', 'models': ['m'], 'extra': True},
            {'api': 'ui-admin-token', 'provider': 'x', 'base_url': 'https://example.com/v1', 'models': ['m']},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = await self.client.post('/_audit/api/setup', json=payload, headers=self.auth)
                self.assertEqual(response.status, 400)
                self.assertFalse(self.config_path.exists())


if __name__ == '__main__':
    unittest.main()
