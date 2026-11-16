"""Local write regressions: real adapter/URL validation, mocked native transport only."""
import copy
import test_management as workflow


class WriteSafetyTests(workflow.ManagementTests):
    async def test_real_local_write_workflow_and_rollback(self):
        before = copy.deepcopy(self.config)
        self.assertTrue((await (await self.call()).json())['native_writes_enabled'])
        self.assertEqual((await self.post({'action':'provider','provider':'custom',
            'base_url':'https://example.org/v1/chat/completions','api':'synthetic-key',
            'models':['paid']})).status, 200)
        self.assertEqual((await self.post({'action':'pool','name':'work','models':['initial/m']})).status, 200)
        issued = await self.post({'action':'client','name':'laptop','pool':'work'})
        self.assertEqual(issued.status, 201)
        self.assertTrue((await issued.json())['client_api_key'])
        self.assertEqual((await self.post({'action':'pool','name':'work','models':['custom/paid']})).status, 200)
        self.assertEqual(self.config['api_keys'][-1]['model'], ['custom/paid'])
        self.assertEqual((await self.post({'action':'rollback'})).status, 200)
        self.assertEqual(self.config['api_keys'][-1]['model'], ['initial/m'])
        self.assertEqual((await (await self.call()).json())['pools']['work'], ['initial/m'])
        self.assertEqual(len(self.patches), 4)
        self.assertEqual(self.config['api_keys'][0], before['api_keys'][0])

# Inherited workflows also run with the real URL validator and mocked native transport.
