import copy
from unittest.mock import AsyncMock, patch
from test_management import ManagementTests


class BlockerTests(ManagementTests):
    async def test_edit_cannot_leave_external_authorization_dangling(self):
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m']})
        before=copy.deepcopy(self.config)
        response=await self.post(dict(action='provider',provider='initial',base_url='https://example.org/v1',api='',models=['other']))
        self.assertEqual(response.status,409)
        self.assertEqual(self.config,before)
        self.assertEqual(self.patches,[])

    async def test_rollback_rejects_later_local_pool_change(self):
        await self.post(dict(action='provider',provider='other',base_url='https://example.org/v1',api='synthetic',models=['n']))
        await self.post(dict(action='pool',name='later',models=['other/n']))
        before=copy.deepcopy(self.config)
        response=await self.post({'action':'rollback'})
        self.assertEqual(response.status,409)
        self.assertIn('pool', (await response.json())['error'].lower())
        self.assertEqual(self.config,before)
        self.assertEqual((await (await self.call()).json())['pools']['later'],['other/n'])
        self.assertEqual(len(self.patches),1)

    async def test_bootstrap_key_selective_sync_requires_confirmation(self):
        self.config['providers'][0]['model']=['m','keep']
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m','initial/keep']})
        self.config['api_keys'].append({'api':'synthetic-other','role':'user','model':['elsewhere/x']})
        before=copy.deepcopy(self.config)
        with patch('management.fetch_models',new=AsyncMock(return_value=['keep','new'])):
            response=await self.post({'action':'provider_refresh','provider':'initial'})
        self.assertEqual(response.status,200)
        preview=await response.json()
        self.assertTrue(preview['clients'])
        self.assertNotIn('synthetic',str(preview))
        self.assertEqual(self.config,before)
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,200)
        self.assertEqual(self.config['api_keys'][1]['model'],['initial/keep'])
        self.assertEqual(self.config['api_keys'][2],before['api_keys'][2])
        self.assertEqual(self.config['api_keys'][0],before['api_keys'][0])
        self.assertEqual((await self.post({'action':'rollback'})).status,200)
        self.assertEqual(self.config,before)

    async def test_external_empty_or_ambiguous_rules_never_publish(self):
        for models in ([], ['all'], ['initial/*'], ['m']):
            self.config['api_keys']=self.config['api_keys'][:1]+[{'api':'synthetic-initial','model':models}]
            response=await self.post({'action':'provider_delete','provider':'initial'})
            self.assertEqual(response.status,409,models)
        self.assertEqual(self.patches,[])

    async def test_delete_syncs_external_key_without_grants(self):
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m','other/keep']})
        preview=await (await self.post({'action':'provider_delete','provider':'initial'})).json()
        self.assertIn('preview',preview)
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,200)
        self.assertEqual(self.config['api_keys'][1]['model'],['other/keep'])

    async def test_last_external_rule_revokes_key_only_on_confirmation(self):
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m']})
        preview=await (await self.post({'action':'provider_delete','provider':'initial'})).json()
        self.assertTrue(preview.get('revoked_clients'))
        self.assertEqual(len(self.config['api_keys']),2)
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,200)
        self.assertEqual(len(self.config['api_keys']),1)
        self.assertEqual((await self.post({'action':'rollback'})).status,200)
        self.assertEqual(self.config['api_keys'][1]['model'],['initial/m'])

    async def test_external_sync_stale_and_cancelled_preview_do_not_change_keys(self):
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m']})
        preview=await (await self.post({'action':'provider_delete','provider':'initial'})).json()
        self.assertEqual(self.patches,[])
        self.config['api_keys'][1]['model'].append('other/keep')
        before=copy.deepcopy(self.config)
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,409)
        self.assertEqual(self.config,before)
        self.assertEqual(self.patches,[])

    async def test_failed_external_sync_can_roll_back_without_expanding_rules(self):
        self.config['api_keys'].append({'api':'synthetic-initial','model':['initial/m']})
        before=copy.deepcopy(self.config)
        preview=await (await self.post({'action':'provider_delete','provider':'initial'})).json()
        self.fail_updates=True
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,503)
        self.assertEqual(self.config,before)
        self.fail_updates=False
        self.assertEqual((await self.post({'action':'rollback'})).status,200)
        self.assertEqual(self.config,before)



import unittest


class JournalMigrationTests(unittest.TestCase):
    def test_existing_journal_schema_migrates_without_losing_versions(self):
        import json
        import sqlite3
        import tempfile
        from pathlib import Path
        from management import storage
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'management.sqlite3')
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE versions(id INTEGER PRIMARY KEY, before_config TEXT NOT NULL, after_config TEXT NOT NULL, pools TEXT NOT NULL, status TEXT NOT NULL)')
            db.execute('INSERT INTO versions VALUES(1,?,?,?,?)',('{}','{}',json.dumps({'work':['initial/m']}),'applied'))
            db.commit(); db.close()
            db=storage(path)
            self.assertEqual(db.execute('SELECT id,pools,status,after_pools FROM versions').fetchone(),
                             (1,json.dumps({'work':['initial/m']}),'applied',None))
            db.close()
            db=storage(path)  # Reopening is idempotent.
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],1)
            db.close()
