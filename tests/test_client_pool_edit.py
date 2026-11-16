"""Single-client pool edits: real adapter/journal, synthetic native transport only."""
import copy
import hashlib
import json
import sqlite3
import unittest
from typing import Any, cast
from unittest.mock import patch

import management
import test_management as workflow


class ClientPoolEditTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the synthetic transport harness, not its inherited test cases.
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
            {'api': 'synthetic-laptop', 'role': 'console:old:laptop',
             'model': ['initial/m'], 'preferences': {'SCHEDULING_ALGORITHM': 'round_robin', 'custom': [1]},
             'rate_limit': 12, 'extra': {'nested': True}},
            {'api': 'synthetic-peer', 'role': 'console:old:peer', 'model': ['initial/m']},
            {'api': 'synthetic-target-peer', 'role': 'console:new:laptop-extra', 'model': ['initial/other']},
            {'api': 'synthetic-external', 'role': 'external', 'model': ['initial/m']},
        ])

    async def test_rejects_empty_pool_without_publication(self):
        with self.db:
            self.db.execute('UPDATE managed_pools SET models=? WHERE name=?', ('[]', 'new'))
        before = copy.deepcopy(self.config)
        response = await self.post(self.payload())
        self.assertEqual(response.status, 400)
        self.assert_config_unchanged(before)
        self.assertEqual(self.patches, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)

    async def test_rejects_console_shaped_native_admin(self):
        self.config['api_keys'][1]['role'] = 'console:administrator:laptop'
        before = copy.deepcopy(self.config)
        self.assertEqual((await self.post(self.payload())).status, 400)
        self.assert_config_unchanged(before)
        self.assertEqual(self.patches, [])

    async def test_rejects_invalid_fields_unknown_pool_and_unknown_client(self):
        before = copy.deepcopy(self.config)
        invalid = [
            self.payload(pool='missing'), self.payload(name='missing'),
            self.payload(name='Laptop'), self.payload(name='lap'),
            self.payload(name='admin'), self.payload(name='bad:name'),
            self.payload(name=None), self.payload(pool=[]), self.payload(pool=''),
            self.payload(api='forbidden'), self.payload(models=['initial/m']),
            {'action': 'client_update', 'name': 'laptop'},
            {'action': 'client_update', 'pool': 'new'},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual((await self.post(payload)).status, 400)
                self.assert_config_unchanged(before)
        self.assertEqual(self.patches, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)

    async def test_rejects_external_and_native_admin_clients(self):
        for role in ('external', 'external:old:laptop', 'console:old:laptop:extra', 'admin'):
            with self.subTest(role=role):
                self.config['api_keys'][1]['role'] = role
                before = copy.deepcopy(self.config)
                self.assertEqual((await self.post(self.payload())).status, 400)
                self.assert_config_unchanged(before)
        self.assertEqual(self.patches, [])

    async def test_rejects_ambiguous_names_in_same_or_different_pools(self):
        for role in ('console:old:laptop', 'console:new:laptop'):
            duplicate = {'api': 'synthetic-duplicate', 'role': role, 'model': ['initial/m']}
            self.config['api_keys'].append(duplicate)
            before = copy.deepcopy(self.config)
            self.assertEqual((await self.post(self.payload())).status, 400)
            self.assert_config_unchanged(before)
            self.config['api_keys'].pop()
        self.assertEqual(self.patches, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)

    async def test_rollback_restores_credentials_roles_models_and_index(self):
        before = copy.deepcopy(self.config)
        self.assertEqual((await self.post(self.payload())).status, 200)
        self.assertEqual((await self.post({'action': 'rollback'})).status, 200)
        self.assert_config_unchanged(before)
        view = await (await self.call()).json()
        self.assertIn(('laptop', 'old'), [(c['name'], c['pool']) for c in view['clients']])
        self.assertEqual(view['pools'], self.pools)
        self.assertEqual(self.db.execute('SELECT pool,client_name FROM client_keys WHERE role=?',
                                        ('console:old:laptop',)).fetchone(), ('old', 'laptop'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)
        self.assertEqual(len(self.patches), 2)

    async def test_failed_or_unconfirmed_publication_blocks_writes_until_rollback(self):
        before = copy.deepcopy(self.config)
        for mode in ('fail_updates', 'stale_updates'):
            with self.subTest(mode=mode):
                setattr(self, mode, True)
                response = await self.post(self.payload())
                self.assertEqual(response.status, 503)
                self.assert_config_unchanged(before)
                self.assertEqual(self.db.execute('SELECT status FROM versions').fetchone()[0], 'pending')
                self.assertEqual((await self.post(self.payload(name='peer'))).status, 409)
                setattr(self, mode, False)
                self.assertEqual((await self.post({'action': 'rollback'})).status, 200)
                self.assert_config_unchanged(before)
                self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 0)
                raw = await response.text()
                self.assertTrue(all(k['api'] not in raw for k in before['api_keys']))

    async def test_failed_rollback_retains_pending_journal_for_retry(self):
        before = copy.deepcopy(self.config)
        self.assertEqual((await self.post(self.payload())).status, 200)
        updated = copy.deepcopy(self.config)
        self.fail_updates = True
        self.assertEqual((await self.post({'action': 'rollback'})).status, 503)
        self.assert_config_unchanged(updated)
        self.assertEqual(self.db.execute('SELECT status FROM versions').fetchone()[0], 'pending')
        self.assertEqual((await self.post(self.payload(name='peer'))).status, 409)
        self.fail_updates = False
        self.assertEqual((await self.post({'action': 'rollback'})).status, 200)
        self.assert_config_unchanged(before)

    async def test_unauthenticated_edit_never_reaches_native_publication(self):
        before = copy.deepcopy(self.config)
        self.assertEqual((await self.call('POST', self.payload(), headers={})).status, 401)
        self.assert_config_unchanged(before)
        self.assertEqual(self.patches, [])

    def payload(self, **overrides):
        return {'action': 'client_update', 'name': 'laptop', 'pool': 'new', **overrides}

    def assert_config_unchanged(self, before):
        # Avoid exposing even synthetic credentials in assertion diffs.
        self.assertTrue(self.config == before, 'native configuration changed unexpectedly')

    async def test_moves_exact_client_without_rotating_or_changing_other_fields(self):
        before = copy.deepcopy(self.config)
        with patch('management.secrets.token_urlsafe', side_effect=AssertionError('must not mint a key')):
            response = await self.post(self.payload())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'updated': True})
        expected = copy.deepcopy(before)
        expected['api_keys'][1].update(role='console:new:laptop', model=['initial/other'])
        self.assertTrue(self.config == expected, 'only the selected role/models may change')
        self.assertEqual(len(self.patches), 1)
        journal = self.db.execute('SELECT before_config,after_config,status,after_pools FROM versions').fetchall()
        self.assertEqual(len(journal), 1)
        self.assertTrue(json.loads(journal[0][0]) == before)
        self.assertTrue(json.loads(journal[0][1]) == expected)
        self.assertEqual(journal[0][2], 'applied')
        self.assertEqual(json.loads(journal[0][3]), self.pools)
        view = await (await self.call()).json()
        self.assertIn(('laptop', 'new'), [(c['name'], c['pool']) for c in view['clients']])
        fingerprint = hashlib.sha256(before['api_keys'][1]['api'].encode()).hexdigest()[:16]
        self.assertEqual(self.db.execute('SELECT role,pool,client_name FROM client_keys WHERE prefix=?',
                                        (fingerprint,)).fetchone(), ('console:new:laptop', 'new', 'laptop'))
        raw = json.dumps(view) + await response.text()
        self.assertTrue(all(k['api'] not in raw for k in before['api_keys']))
