import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from audit_console import Settings, create_app

class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {'providers':[{'provider':'initial','base_url':'https://example.com/v1/chat/completions','api':'provider-secret','model':['m']}], 'api_keys':[{'api':'native-admin','role':'admin','model':['all']}]}
        self.patches=[]; self.fail_updates=False; self.stale_updates=False
        from unittest.mock import patch
        from contextlib import asynccontextmanager
        import json
        class Session:
            @asynccontextmanager
            async def request(inner, method, url, **kw):
                if method == 'POST':
                    self.patches.append(kw['json'])
                    if not self.fail_updates and not self.stale_updates: self.config.update(kw['json'])
                data = json.dumps({'api_config':self.config} if method == 'GET' else {'message':'API config updated'}).encode()
                class Content:
                    async def iter_chunked(inner, size): yield data
                class Response:
                    status=503 if method == 'POST' and self.fail_updates else 200
                    content=Content()
                yield Response()
            async def close(inner): pass
        self.patch=patch('audit_console.ClientSession',return_value=Session()); self.patch.start()
        self.app=create_app(Settings('http://localhost:8000','ui-admin',db=':memory:',management_key='native-admin',provider_hosts=('example.org',)))
        self.context=self.app.cleanup_ctx[0](self.app); await anext(self.context)
        self.auth={'Authorization':'Bearer ui-admin'}
    async def asyncTearDown(self):
        try: await anext(self.context)
        except StopAsyncIteration: pass
        self.patch.stop()
    async def call(self, method='GET', body=None, headers=None):
        from aiohttp.test_utils import make_mocked_request
        from unittest.mock import AsyncMock
        import json
        r=make_mocked_request(method,'/_audit/api/manage',headers=self.auth if headers is None else headers,app=self.app)
        r.json=AsyncMock(return_value=body)
        r._match_info=await self.app.router.resolve(r)
        try: response=await self.app.middlewares[0](r,r.match_info.handler)
        except web.HTTPException as error: response=error
        class Result:
            status=response.status
            async def json(inner): return json.loads(response.text)
            async def text(inner): return response.text
        return Result()
    async def post(self,body): return await self.call('POST',body)
    async def test_authenticated_provider_submission_needs_no_host_file_edit(self):
        # Host is absent from the legacy environment list; submission grants trust.
        result=await self.post({'action':'provider','provider':'trusted',
            'base_url':'https://new.example/v1','api':'synthetic','models':['m']})
        self.assertEqual(result.status,200)
        self.assertEqual(self.config['providers'][-1]['provider'],'trusted')

    async def test_create_and_update_destinations_without_dns(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(
                side_effect=AssertionError('validation must not resolve DNS'))) as dns:
            for url in ('https://openrouter.ai/api/v1', 'http://198.18.0.20:1234/v1',
                        'http://localhost:1/v1', 'https://10.0.0.1:65535/v1',
                        'https://[::1]:8443/v1', 'http://provider.internal:8080/v1'):
                for provider, key in (('custom', 'synthetic'), ('initial', '')):
                    response = await self.post(dict(action='provider', provider=provider,
                        base_url=url, api=key, models=['vendor/model:free', {'source:free':'alias:free'}]))
                    self.assertEqual(response.status, 200)
                    saved = next(p for p in self.config['providers'] if p['provider'] == provider)
                    self.assertEqual(saved['base_url'], url)
                    self.assertEqual(saved['model'], ['vendor/model:free', {'source:free':'alias:free'}])
            dns.assert_not_awaited()

    async def test_provider_pool_binding_sync_and_redaction(self):
        r=await self.post({'action':'provider','provider':'custom','base_url':'https://example.org/v1/chat/completions','api':'paid-secret','models':['paid-model']})
        self.assertEqual(r.status,200)
        r=await self.post({'action':'pool','name':'work','models':['custom/paid-model']})
        self.assertEqual(r.status,200)
        r=await self.post({'action':'client','name':'laptop','pool':'work'})
        self.assertEqual(r.status,201); key=(await r.json())['client_api_key']
        self.assertEqual(self.config['api_keys'][-1]['model'],['custom/paid-model'])
        r=await self.post({'action':'pool','name':'work','models':['initial/m']})
        self.assertEqual(r.status,200); self.assertEqual(self.config['api_keys'][-1]['model'],['initial/m'])
        r=await self.call()
        raw=await r.text()
        for secret in ['paid-secret','provider-secret','native-admin',key]: self.assertNotIn(secret,raw)
        self.assertEqual((await self.post({'action':'revoke','name':'laptop'})).status,200)
        self.assertEqual(len(self.config['api_keys']),1)
    async def test_rollback_restores_native_and_pool_binding(self):
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        await self.post({'action':'client','name':'laptop','pool':'work'})
        self.assertEqual(len(self.config['api_keys']),2)
        result=await self.post({'action':'rollback'})
        self.assertEqual(result.status,200)
        self.assertEqual(len(self.config['api_keys']),1)
        self.assertEqual((await (await self.call()).json())['clients'],[])

    async def test_edit_provider_preserves_credentials_and_guards_pool_models(self):
        self.assertEqual((await self.post({'action':'provider','provider':'initial','base_url':'https://example.org/v1/chat/completions','api':'','models':[{'upstream-paid':'public-paid'}]})).status,200)
        self.assertEqual(self.config['providers'][0]['api'],'provider-secret')
        self.assertEqual((await self.post({'action':'pool','name':'paid','models':['initial/public-paid']})).status,200)
        self.assertEqual((await self.post({'action':'provider','provider':'initial','base_url':'https://example.org/v1/chat/completions','api':'','models':['other']})).status,409)

    async def test_bare_upstream_name_keeps_existing_alias_for_pool(self):
        self.config['providers'][0]['model']=[{'up/x':'initial/x'}]
        self.assertEqual((await self.post({'action':'pool','name':'free','models':['initial/initial/x']})).status,200)
        self.assertEqual((await self.post({'action':'provider','provider':'initial','base_url':'https://example.org/v1/chat/completions','api':'','models':['up/x']})).status,200)
        self.assertEqual(self.config['providers'][0]['model'],[{'up/x':'initial/x'}])

    async def test_source_rename_with_same_basename_keeps_public_alias(self):
        # A typo'd channel id (free/model-a) corrected to vendor/model-a: same model
        # basename, so the pooled public alias must survive the save.
        self.config['providers'][0]['model']=[{'free/model-a':'initial/model-a:free'}]
        self.assertEqual((await self.post({'action':'pool','name':'free','models':['initial/initial/model-a:free']})).status,200)
        self.assertEqual((await self.post({'action':'provider','provider':'initial','base_url':'https://example.org/v1/chat/completions','api':'','models':['nvidia/model-a:free']})).status,200)
        self.assertEqual(self.config['providers'][0]['model'],[{'nvidia/model-a:free':'initial/model-a:free'}])

    async def test_pool_conflict_reports_actionable_message(self):
        self.config['providers'][0]['model']=[{'up/x':'initial/x'},{'up/y':'initial/y'}]
        self.assertEqual((await self.post({'action':'pool','name':'free','models':['initial/initial/x']})).status,200)
        result=await self.post({'action':'provider','provider':'initial','base_url':'https://example.org/v1/chat/completions','api':'','models':['up/y']})
        self.assertEqual(result.status,409)
        message=(await result.json())['error']
        self.assertIn('仍被模型池使用',message)
        self.assertIn('initial/initial/x',message)

    async def test_client_names_cannot_gain_native_admin_role(self):
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        for client in ['admin','myADMINkey','admin-user']:
            self.assertEqual((await self.post({'action':'client','name':client,'pool':'work'})).status,400)
        self.assertEqual((await self.post({'action':'pool','name':'administrator','models':['initial/m']})).status,400)
        self.assertEqual(len(self.config['api_keys']),1)

    async def test_native_credentials_copied_into_model_names_are_redacted(self):
        self.config['providers'][0]['model']=['copy-provider-secret']
        raw=await (await self.call()).text()
        self.assertNotIn('provider-secret',raw)

    async def test_failed_rollback_blocks_followup_writes(self):
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        await self.post({'action':'client','name':'laptop','pool':'work'})
        self.fail_updates=True
        self.assertEqual((await self.post({'action':'rollback'})).status,503)
        self.assertEqual((await self.post({'action':'revoke','name':'laptop'})).status,409)
        self.fail_updates=False; self.stale_updates=False
        self.assertEqual((await self.post({'action':'rollback'})).status,200)
        self.assertEqual(len(self.config['api_keys']),1)

    async def test_pool_updates_do_not_expand_other_client_authorization(self):
        for pool,models in [('one',['initial/m']),('two',['initial/m'])]:
            await self.post({'action':'pool','name':pool,'models':models})
            await self.post({'action':'client','name':pool+'-client','pool':pool})
        await self.post({'action':'provider','provider':'custom','base_url':'https://example.org/v1/chat/completions','api':'paid-secret','models':['paid-model']})
        await self.post({'action':'pool','name':'one','models':['custom/paid-model']})
        rules={k.get('role'):k['model'] for k in self.config['api_keys']}
        self.assertEqual(rules['console:one:one-client'],['custom/paid-model'])
        self.assertEqual(rules['console:two:two-client'],['initial/m'])
        self.assertEqual(rules['admin'],['all'])

    async def test_external_config_change_blocks_rollback(self):
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        self.config['providers'][0]['model'].append('external')
        self.assertEqual((await self.post({'action':'rollback'})).status,409)
        self.assertEqual(self.patches,[])

    async def test_unauthenticated_provider_rejected_before_validation_or_native_mutation(self):
        from unittest.mock import patch
        with patch('management.public_url') as validate:
            result=await self.call('POST',{'action':'provider','provider':'custom',
                'base_url':'https://new.example/v1','api':'synthetic','models':['m']},headers={})
            self.assertEqual(result.status,401)
            validate.assert_not_called()
        self.assertEqual(self.patches,[])

    async def test_native_success_without_published_config_is_not_reported_success(self):
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        self.stale_updates=True
        response=await self.post({'action':'client','name':'laptop','pool':'work'})
        self.assertEqual(response.status,503)
        self.assertNotIn('client_api_key',await response.text())
        self.assertEqual((await self.post({'action':'client','name':'another','pool':'work'})).status,409)

    async def test_security_and_invalid_inputs(self):
        self.assertEqual((await self.call(headers={})).status,401)
        r=await self.call('POST',{},headers={**self.auth,'Origin':'https://evil.example'})
        self.assertEqual(r.status,403)
        for url in ['ftp://example.com/v1','https://user:pass@example.com/v1','http://localhost:0/v1']:
            self.assertEqual((await self.post({'action':'provider','provider':'bad','base_url':url,'api':'secret','models':['m']})).status,400)
        for models in [['all'],['initial/*'],['missing/m'],[]]:
            self.assertEqual((await self.post({'action':'pool','name':'x','models':models})).status,400)
        self.assertEqual(self.patches,[])

class ManagementSecurityUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_syntax_only_without_dns(self):
        from unittest.mock import AsyncMock, patch
        from management import public_url
        import asyncio
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(
                side_effect=AssertionError('validation must not resolve DNS'))) as dns:
            for url in ('https://openrouter.ai/api/v1', 'http://198.18.0.20:1234/v1',
                        'http://localhost:1/v1', 'https://10.0.0.1:65535/v1',
                        'https://[::1]:8443/v1', 'http://provider.internal/v1',
                        'https://169.254.169.254/v1', 'http://224.0.0.1/v1'):
                self.assertEqual(await public_url(url), url)
            dns.assert_not_awaited()
        for url in ('ftp://example.com', 'https://', 'https://@example.com',
                    'https://user:secret@example.com', 'https://example.com?q=x',
                    'https://example.com/#x', 'http://localhost:0', 'http://localhost:65536',
                    'http://localhost:bad', 'http://example.com/\\foo', 'http://example.com/\nfoo'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await public_url(url)

    async def test_management_storage_is_private_separate_and_refuses_symlink(self):
        import tempfile, os
        from pathlib import Path
        from management import storage
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audit.sqlite3.management'
            db=storage(str(path))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(db.execute('PRAGMA secure_delete').fetchone()[0],1)
            db.close()
            link=Path(directory)/'link'; link.symlink_to(path)
            with self.assertRaises(OSError): storage(str(link))
        self.assertEqual(Settings.from_env({'AUDIT_UPSTREAM':'http://localhost','AUDIT_ADMIN_TOKEN':'ui'}).provider_hosts,())
        self.assertEqual(Settings.from_env({'AUDIT_UPSTREAM':'http://localhost','AUDIT_ADMIN_TOKEN':'ui','AUDIT_PROVIDER_HOSTS':' Example.Org, api.example.com '}).provider_hosts,('example.org','api.example.com'))
