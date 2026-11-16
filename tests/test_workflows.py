from test_management import ManagementTests
from unittest.mock import patch, AsyncMock

class WorkflowTests(ManagementTests):
    async def test_preview_confirmation_preserves_alias_and_syncs(self):
        self.config['providers'][0]['model']=[{'source':'alias'},'gone']
        await self.post({'action':'pool','name':'work','models':['initial/alias','initial/gone']})
        await self.post({'action':'client','name':'laptop','pool':'work'})
        with patch('management.fetch_models',new=AsyncMock(return_value=['source','unselected'])):
            response=await self.post({'action':'provider_refresh','provider':'initial'})
        self.assertEqual(response.status,200)
        preview=await response.json()
        self.assertEqual(preview['removed'],['gone'])
        self.assertEqual(preview['pools'],['work'])
        self.assertEqual(self.config['providers'][0]['model'],[{'source':'alias'},'gone'])
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,200)
        self.assertEqual(self.config['providers'][0]['model'],[{'source':'alias'}])
        self.assertEqual(self.config['api_keys'][-1]['model'],['initial/alias'])
        self.assertEqual((await (await self.call()).json())['pools']['work'],['initial/alias'])

    async def test_failure_and_empty_pool_never_publish(self):
        with patch('management.fetch_models',new=AsyncMock(side_effect=ValueError())):
            self.assertEqual((await self.post({'action':'provider_refresh','provider':'initial'})).status,409)
        await self.post({'action':'pool','name':'work','models':['initial/m']})
        result=await self.post({'action':'provider_delete','provider':'initial'})
        self.assertEqual(result.status,409)
        self.assertEqual(self.patches,[])

    async def test_delete_preview_stale_confirmation(self):
        result=await self.post({'action':'provider_delete','provider':'initial'})
        self.assertEqual(result.status,200)
        preview=await result.json()
        self.config['providers'][0]['model'].append('external')
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,409)
        self.assertEqual(self.patches,[])

    async def test_incomplete_catalog_rejected(self):
        from model_discovery import model_ids
        for payload in ({'data':[{'id':'m'}],'has_more':True}, {'data':[{'id':'m'}],'next':'cursor'}, {'data':[{'id':'m'}],'total':2}):
            with self.assertRaises(ValueError): model_ids(payload)

    async def test_unmanaged_key_blocks_removal(self):
        self.config['api_keys'].append({'api':'external','role':'user','model':['m']})
        response=await self.post({'action':'provider_delete','provider':'initial'})
        self.assertEqual(response.status,409)
        self.assertEqual(self.patches,[])


    async def test_delete_confirm_and_rollback(self):
        preview=await (await self.post({'action':'provider_delete','provider':'initial'})).json()
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,200)
        self.assertEqual(self.config['providers'],[])
        self.assertEqual((await self.post({'action':'rollback'})).status,200)
        self.assertEqual(self.config['providers'][0]['api'],'provider-secret')

    async def test_non_openai_and_multikey_refresh_blocked(self):
        for value in ({'engine':'anthropic'}, {'api':['one','two']}):
            self.config['providers'][0].update(value)
            with patch('management.fetch_models',new=AsyncMock()) as fetch:
                self.assertEqual((await self.post({'action':'provider_refresh','provider':'initial'})).status,409)
                fetch.assert_not_awaited()
        self.assertEqual(self.patches,[])

    async def test_failed_confirmation_retains_pool_metadata(self):
        self.config['providers'][0]['model']=['m','other']
        await self.post({'action':'pool','name':'work','models':['initial/m','initial/other']})
        await self.post({'action':'client','name':'laptop','pool':'work'})
        with patch('management.fetch_models',new=AsyncMock(return_value=['m'])):
            preview=await (await self.post({'action':'provider_refresh','provider':'initial'})).json()
        self.fail_updates=True
        self.assertEqual((await self.post({'action':'confirm','preview':preview['preview']})).status,503)
        self.assertEqual((await (await self.call()).json())['pools']['work'],['initial/m','initial/other'])
        self.assertEqual((await self.post({'action':'revoke','name':'laptop'})).status,409)

    async def test_duplicate_alias_cannot_prune_authorization(self):
        self.config['providers'][0]['model']=[{'source':'same'},{'other':'same'}]
        with patch('management.fetch_models',new=AsyncMock(return_value=['source'])):
            self.assertEqual((await self.post({'action':'provider_refresh','provider':'initial'})).status,409)
        self.assertEqual(self.patches,[])
