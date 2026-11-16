"""Native key binding, deletion, masking and clipboard retrieval; synthetic transport only."""
import copy
import json
import sqlite3
import unittest
from typing import Any, cast
from unittest.mock import patch

import management
import test_management as workflow


class NativeBindingTests(unittest.IsolatedAsyncioTestCase):
    db: sqlite3.Connection
    config: dict[str, Any]
    patches: list[dict[str, Any]]
    call = cast(Any, workflow.ManagementTests.call)
    post = cast(Any, workflow.ManagementTests.post)
    asyncTearDown = cast(Any, workflow.ManagementTests.asyncTearDown)

    async def asyncSetUp(self):
        real_storage = management.storage

        def capture_storage(path):
            self.db = real_storage(path)
            return self.db

        with patch('management.storage', side_effect=capture_storage):
            await workflow.ManagementTests.asyncSetUp(cast(Any, self))
        self.config['providers'][0]['model'] = ['m', 'other']
        self.pools = {'old': ['initial/m'], 'new': ['initial/other']}
        with self.db:
            self.db.executemany('INSERT INTO managed_pools VALUES(?,?)',
                                [(key, json.dumps(value)) for key, value in self.pools.items()])
        self.config['api_keys'].extend([
            {'api': 'synthetic-laptop', 'role': 'console:old:laptop', 'model': ['initial/m']},
            {'api': 'synthetic-external', 'role': 'external', 'model': ['initial/m']},
            {'api': 'synthetic-unlabelled', 'role': '', 'model': ['initial/m']},
        ])

    def external(self, role):
        return next(k for k in self.config['api_keys'] if k.get('role') == role)

    def assert_no_plaintext(self, raw):
        for row in self.config['api_keys']:
            self.assertNotIn(row['api'], raw)
            if len(row['api']) > 12:
                self.assertNotIn(row['api'][6:-4], raw)
        self.assertNotIn('provider-secret', raw)

    # --- masking ---------------------------------------------------------
    def test_mask_keeps_only_outer_characters(self):
        self.assertEqual(management.mask('abcdefghijklmnopqrstuvwxyz'), 'abcdef****wxyz')
        self.assertEqual(management.mask('abcdefghijklmnopqrstuvwxyz', short=True), 'abc****yz')
        self.assertEqual(management.mask('short'), '******')
        self.assertEqual(management.mask(''), '')
        self.assertEqual(management.mask(None), '')

    def test_admin_mask_is_shorter_than_client_mask(self):
        self.assertEqual(management.mask('native-admin'), '******')
        long_admin = 'a' * 40
        self.assertEqual(management.mask(long_admin, short=True), 'aaa****aa')
        self.assertEqual(management.mask(long_admin), 'aaaaaa****aaaa')

    # --- view ------------------------------------------------------------
    async def test_view_exposes_masks_and_binding_without_plaintext(self):
        view = await (await self.call()).json()
        self.assertEqual(view['clients'][0]['name'], 'laptop')
        self.assertEqual(view['clients'][0]['masked'], 'synthe****ptop')
        labels = {(row['role'], row['label'], row['admin'], row['copyable']) for row in view['external_clients']}
        self.assertIn(('external', 'external', False, True), labels)
        self.assertIn(('', '未命名', False, True), labels)
        self.assertIn(('admin', 'admin', True, True), labels)
        unbound = next(row for row in view['external_clients'] if row['role'] == 'external')
        self.assertEqual(unbound['bound_pool'], 'old')
        self.assert_no_plaintext(await (await self.call()).text())

    async def test_view_reports_no_binding_for_partial_pool_membership(self):
        self.external('external')['model'] = ['initial/m', 'initial/other']
        view = await (await self.call()).json()
        row = next(r for r in view['external_clients'] if r['role'] == 'external')
        self.assertIsNone(row['bound_pool'])

    # --- client_key / native_key ----------------------------------------
    async def test_client_key_returns_only_the_requested_client_secret(self):
        response = await self.post({'action': 'client_key', 'name': 'laptop'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['key'], 'synthetic-laptop')

    async def test_client_key_rejects_unknown_client_and_extra_fields(self):
        for body in ({'action': 'client_key', 'name': 'missing'},
                     {'action': 'client_key', 'name': 'laptop', 'role': 'admin'},
                     {'action': 'client_key'}):
            self.assertEqual((await self.post(body)).status, 400)

    async def test_native_key_returns_external_secret_only(self):
        response = await self.post({'action': 'native_key', 'role': 'external'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['key'], 'synthetic-external')
        self.assertEqual((await self.post({'action': 'native_key', 'role': ''})).status, 200)

    async def test_native_key_returns_admin_credential_when_role_matches(self):
        # Operator chose to allow copying the native admin key from the console.
        response = await self.post({'action': 'native_key', 'role': 'admin'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['key'], 'native-admin')

    async def test_native_key_rejects_unknown_roles_and_console_clients(self):
        before = copy.deepcopy(self.config)
        for body in ({'action': 'native_key', 'role': 'missing'},
                     {'action': 'native_key', 'role': 'console:old:laptop'},
                     {'action': 'native_key', 'role': 5},
                     {'action': 'native_key'}):
            self.assertEqual((await self.post(body)).status, 400)
        self.assertEqual(self.config, before)
        self.assertEqual(self.patches, [])

    # --- native_pool -----------------------------------------------------
    async def test_native_pool_binds_pool_models_and_keeps_role(self):
        response = await self.post({'action': 'native_pool', 'role': 'external', 'pool': 'new'})
        self.assertEqual(response.status, 200)
        entry = self.external('external')
        self.assertEqual(entry['model'], ['initial/other'])
        self.assertEqual(entry['role'], 'external')
        self.assertEqual(self.db.execute('SELECT status FROM versions ORDER BY id DESC LIMIT 1').fetchone()[0], 'applied')
        view = await (await self.call()).json()
        row = next(r for r in view['external_clients'] if r['role'] == 'external')
        self.assertEqual(row['bound_pool'], 'new')

    async def test_native_pool_rejects_bad_payloads_without_publication(self):
        before = copy.deepcopy(self.config)
        for body in ({'action': 'native_pool', 'role': 'external', 'pool': 'missing'},
                     {'action': 'native_pool', 'role': 'missing', 'pool': 'new'},
                     {'action': 'native_pool', 'role': 'console:old:laptop', 'pool': 'new'},
                     {'action': 'native_pool', 'role': 'external'}):
            self.assertEqual((await self.post(body)).status, 400)
        with self.db:
            self.db.execute('UPDATE managed_pools SET models=? WHERE name=?', ('[]', 'new'))
        self.assertEqual((await self.post({'action': 'native_pool', 'role': 'external', 'pool': 'new'})).status, 400)
        self.assertEqual(self.config, before)
        self.assertEqual(self.patches, [])

    async def test_native_pool_restricts_admin_key_without_dropping_role(self):
        response = await self.post({'action': 'native_pool', 'role': 'admin', 'pool': 'new'})
        self.assertEqual(response.status, 200)
        admin = next(k for k in self.config['api_keys'] if k.get('role') == 'admin')
        self.assertEqual(admin['model'], ['initial/other'])
        self.assertEqual(admin['api'], 'native-admin')
        self.assertEqual((await self.call()).status, 200)
        view = await (await self.call()).json()
        row = next(r for r in view['external_clients'] if r['role'] == 'admin')
        self.assertEqual(row['bound_pool'], 'new')
        self.assertTrue(row['admin'])

    # --- native_delete ---------------------------------------------------
    async def test_native_delete_removes_unlabelled_key_and_keeps_admin(self):
        response = await self.post({'action': 'native_delete', 'role': ''})
        self.assertEqual(response.status, 200)
        roles = [k.get('role', '') for k in self.config['api_keys']]
        self.assertNotIn('', roles)
        self.assertIn('external', roles)
        self.assertEqual(len(self.config['api_keys']), 3)
        self.assertEqual((await self.call()).status, 200)

    async def test_native_delete_refuses_admin_and_console_keys(self):
        before = copy.deepcopy(self.config)
        for body in ({'action': 'native_delete', 'role': 'admin'},
                     {'action': 'native_delete', 'role': 'console:old:laptop'},
                     {'action': 'native_delete', 'role': 'missing'}):
            self.assertEqual((await self.post(body)).status, 400)
        self.assertEqual(self.config, before)
        self.assertEqual(self.patches, [])

    async def test_native_delete_of_bound_external_key_updates_view(self):
        self.assertEqual((await self.post({'action': 'native_delete', 'role': 'external'})).status, 200)
        view = await (await self.call()).json()
        self.assertNotIn('external', [r['role'] for r in view['external_clients']])
        self.assertIn('admin', [r['role'] for r in view['external_clients']])


if __name__ == '__main__':
    unittest.main()
