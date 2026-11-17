"""Tests for the per-provider quota/balance readers (usage.py module)."""

import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from audit_console import Settings, create_app
import usage
import usage as usage_mod


class HelperTests(IsolatedAsyncioTestCase):
    def test_num_tolerates_currency_and_separators(self):
        self.assertEqual(usage.num('$1,234.50'), 1234.5)
        self.assertEqual(usage.num('¥88'), 88.0)
        self.assertEqual(usage.num('42%'), 42.0)
        self.assertIsNone(usage.num(True))
        self.assertIsNone(usage.num('n/a'))
        self.assertEqual(usage.num(7), 7.0)

    def test_parse_path_nested_and_indexed(self):
        data = {'data': [{'used': 3, 'total': 12}], 'meta': {'list': [1, 2, 3]}}
        self.assertEqual(usage.parse_path(data, 'data[0].used'), 3)
        self.assertEqual(usage.parse_path(data, 'data[0].total'), 12)
        self.assertEqual(usage.parse_path(data, 'meta.list[2]'), 3)
        self.assertIsNone(usage.parse_path(data, 'data[1].used'))
        self.assertIsNone(usage.parse_path(data, 'data[0].missing'))
        self.assertIsNone(usage.parse_path(data, ''))
        self.assertIsNone(usage.parse_path(data, 'data[0]x'))

    def test_default_mode_by_host(self):
        self.assertEqual(usage.default_mode('https://openrouter.ai/api/v1'), 'openrouter')
        self.assertEqual(usage.default_mode('https://developer.amd.com.cn/radeon/api/v1'), 'amd')
        self.assertEqual(usage.default_mode('https://freesharenow.com/v1'), 'openai_billing')
        self.assertEqual(usage.default_mode('https://minekuai.cn/v1'), 'none')

    def test_openrouter_reading_uses_credits_and_free_quota(self):
        reading = usage.build_openrouter('or', {'data': {'total_credits': 10, 'total_usage': 3.82}},
                                         {'data': {'free_model_daily_requests': {'used': 113, 'limit': 1000, 'remaining': 887}}})
        self.assertTrue(reading['ok'])
        self.assertAlmostEqual(reading['remaining'], 6.18, places=4)
        self.assertAlmostEqual(reading['percent'], 61.8, places=2)
        self.assertEqual(reading['unit'], 'usd')
        self.assertIn('887', reading['detail']['免费模型今日'])

    def test_openai_billing_unlimited_hides_percent(self):
        reading = usage.build_openai_billing('aio', {'hard_limit_usd': 100000000}, {'total_usage': 12235.79})
        self.assertAlmostEqual(reading['used'], 122.3579, places=4)
        self.assertIsNone(reading['percent'])
        self.assertIsNone(reading['remaining'])
        self.assertIn('不限额度', reading['label'])

    def test_billing_daily_window_uses_baseline_and_anchor(self):
        sub, usage = {'hard_limit_usd': 100000000}, {'total_usage': 1255015.4}
        config = {'limit_value': 80, 'reset': 'day', 'unit': 'cny', 'label': ''}
        # Fresh day, baseline captured from the anchor: 80-55.92 = 24.08 used today.
        reading = usage_mod.build_openai_billing('aio', sub, usage, config, baseline=1255015.4 / 100.0 - 24.08)
        self.assertAlmostEqual(reading['used'], 24.08, places=3)
        self.assertAlmostEqual(reading['remaining'], 55.92, places=3)
        self.assertAlmostEqual(reading['percent'], 69.9, places=2)
        self.assertEqual(reading['detail']['周期'], '每日重置')
        self.assertEqual(reading['unit'], 'cny')

    def test_amd_daily_budget_reading(self):
        reading = usage.build_amd('amd', {'data': {'usage': '2.97'}}, {
            'daily_cost_limit_usd': 1, 'daily_cost_used_usd': 0.25, 'daily_cost_remaining_usd': 0.75,
            'rpm_limit': 20, 'all_time': {'requests': 637, 'errors': 21, 'total_tokens': 64000000},
            'daily_reset_timezone': 'Asia/Shanghai'})
        self.assertAlmostEqual(reading['percent'], 75.0, places=3)
        self.assertEqual(reading['detail']['RPM限制'], 20.0)
        self.assertEqual(reading['detail']['累计请求'], 637.0)

    def test_custom_paths_and_cents_scaling(self):
        config = {'used_path': 'data.used', 'total_path': 'data.total', 'remaining_path': '',
                  'unit': 'usd_cents', 'label': '本月额度'}
        reading = usage.build_custom('c', {'data': {'used': 250, 'total': 1000}}, config)
        self.assertAlmostEqual(reading['used'], 2.5)
        self.assertAlmostEqual(reading['total'], 10.0)
        self.assertAlmostEqual(reading['remaining'], 7.5)
        self.assertAlmostEqual(reading['percent'], 75.0)
        self.assertEqual(reading['label'], '本月额度')

    def test_manual_mode_counts_local_tokens_and_honours_anchor(self):
        config = {'mode': 'manual', 'unit': 'tokens', 'limit_value': 50_000_000, 'reset': 'day', 'label': ''}
        reading = usage.build_manual('minekuai', config, 12_000_000, 340)
        self.assertTrue(reading['ok'])
        self.assertAlmostEqual(reading['used'], 12_000_000)
        self.assertAlmostEqual(reading['remaining'], 38_000_000)
        self.assertAlmostEqual(reading['percent'], 76.0, places=3)
        self.assertEqual(reading['label'], '今日额度')
        # An operator-written anchor wins over the local tally (money panels).
        money = {'mode': 'manual', 'unit': 'cny', 'limit_value': 80, 'reset': 'day', 'manual_remaining': 55.92}
        anchored = usage.build_manual('all-in-one', money, 900_000, 42)
        self.assertAlmostEqual(anchored['remaining'], 55.92)
        self.assertAlmostEqual(anchored['percent'], 69.9, places=2)
        self.assertEqual(anchored['detail']['本地已用tokens'], 900_000)
        self.assertEqual(anchored['detail']['本地请求数'], 42)

    def test_manual_money_price_and_missing_limit(self):
        priced = usage.build_manual('x', {'mode': 'manual', 'unit': 'cny', 'limit_value': 80,
                                          'price_per_million': 2.0}, 5_000_000, 10)
        self.assertAlmostEqual(priced['used'], 10.0)
        self.assertAlmostEqual(priced['remaining'], 70.0)
        broken = usage.build_manual('x', {'mode': 'manual', 'unit': 'tokens', 'limit_value': None}, 0, 0)
        self.assertFalse(broken['ok'])
        self.assertIn('限额', broken['error'])

    def test_period_start_is_daily_or_lifetime(self):
        self.assertEqual(usage._period_start('total', 'Asia/Shanghai'), 0.0)
        import time as _time
        start = usage._period_start('day', 'Asia/Shanghai')
        self.assertLessEqual(start, _time.time())
        self.assertGreater(start, _time.time() - 86400 - 1)

    def test_scrub_removes_key_shaped_text(self):
        self.assertNotIn('sk-abcdef123', usage.scrub('boom sk-abcdef123 boom'))


class RouteTests(IsolatedAsyncioTestCase):
    """Routes with a stub aiohttp session serving the native config plus each
    provider's quota endpoints."""

    config_payload = {'api_config': {'providers': [
        {'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'api': 'or-key'},
        {'provider': 'amd', 'base_url': 'https://developer.amd.com.cn/radeon/api/v1', 'api': 'amd-key'},
        {'provider': 'all-in-one', 'base_url': 'https://freesharenow.com/v1', 'api': 'aio-key'},
        {'provider': 'minekuai', 'base_url': 'https://minekuai.cn/v1', 'api': 'mk-key'},
    ]}}

    payloads = {
        'https://openrouter.ai/api/v1/credits': {'data': {'total_credits': 10, 'total_usage': 3.82}},
        'https://openrouter.ai/api/v1/key': {'data': {'free_model_daily_requests': {'used': 113, 'limit': 1000, 'remaining': 887}}},
        'https://developer.amd.com.cn/radeon/api/v1/key': {'data': {'usage': '2.97'}},
        'https://developer.amd.com.cn/radeon/api/v1/usage': {'daily_cost_limit_usd': 1, 'daily_cost_used_usd': 0,
                                                             'daily_cost_remaining_usd': 1, 'rpm_limit': 20},
        'https://freesharenow.com/v1/dashboard/billing/subscription': {'hard_limit_usd': 100000000},
        'https://freesharenow.com/v1/dashboard/billing/usage': {'total_usage': 12235.79},
        'https://minekuai.cn/v1/custom/quota': {'data': [{'used': 5, 'total': 20}]},
    }

    async def asyncSetUp(self):
        payloads, config = self.payloads, self.config_payload

        class Content:
            def __init__(self, body): self.body = body
            async def iter_chunked(self, size):
                yield self.body

        class Response:
            def __init__(self, body, status=200): self.content = Content(body); self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False

        class Session:
            def request(self, method, url, **kw):
                return Response(json.dumps(config).encode())
            def get(self, url, **kw):
                if url.endswith('/v1/api_config'):
                    return Response(json.dumps(config).encode())
                body = payloads.get(url)
                if body is None:
                    return Response(b'{}', status=404)
                return Response(json.dumps(body).encode())
            async def close(self): pass

        self.patch = patch('audit_console.ClientSession', return_value=Session())
        self.patch.start()
        self.app = create_app(Settings('http://localhost:8000', 'ui-admin', db=':memory:',
                                       management_key='native-admin', provider_hosts=('example.org',)))
        self.context = self.app.cleanup_ctx[0](self.app)
        await anext(self.context)
        self.auth = {'Authorization': 'Bearer ui-admin'}

    async def asyncTearDown(self):
        try:
            await anext(self.context)
        except StopAsyncIteration:
            pass
        self.patch.stop()

    async def call(self, method='GET', path='/_audit/api/balances', body=None, headers=None):
        r = make_mocked_request(method, path, headers=self.auth if headers is None else headers, app=self.app)
        r.json = AsyncMock(return_value=body)
        r._match_info = await self.app.router.resolve(r)
        try:
            response = await self.app.middlewares[0](r, r.match_info.handler)
        except web.HTTPException as error:
            response = error

        class Result:
            status = response.status

            async def json(inner):
                return json.loads(getattr(response, 'text', '') or '{}')
        return Result()

    async def test_readings_follow_each_provider_mode(self):
        result = await self.call()
        self.assertEqual(result.status, 200)
        providers = {item['provider']: item for item in (await result.json())['providers']}
        self.assertEqual(providers['openrouter']['mode'], 'openrouter')
        self.assertAlmostEqual(providers['openrouter']['reading']['remaining'], 6.18, places=3)
        self.assertEqual(providers['amd']['mode'], 'amd')
        self.assertAlmostEqual(providers['amd']['reading']['percent'], 100.0, places=3)
        self.assertEqual(providers['all-in-one']['mode'], 'openai_billing')
        self.assertIsNone(providers['all-in-one']['reading']['percent'])
        self.assertEqual(providers['minekuai']['mode'], 'none')
        self.assertIsNone(providers['minekuai']['reading'])

    async def test_custom_mode_saves_and_refreshes(self):
        saved = await self.call('POST', body={'provider': 'minekuai', 'mode': 'custom', 'url': '/v1/custom/quota',
                                             'used_path': 'data[0].used', 'total_path': 'data[0].total',
                                             'remaining_path': '', 'unit': 'usd', 'label': '本月额度'})
        self.assertEqual(saved.status, 200)
        refreshed = await self.call('POST', '/_audit/api/balances/refresh', {'provider': 'minekuai', 'force': True})
        self.assertEqual(refreshed.status, 200)
        entry = (await refreshed.json())['providers'][0]
        self.assertEqual(entry['mode'], 'custom')
        self.assertAlmostEqual(entry['reading']['percent'], 75.0, places=3)
        self.assertAlmostEqual(entry['reading']['remaining'], 15.0)
        self.assertEqual(entry['reading']['label'], '本月额度')

    async def test_rejects_invalid_mode_unit_and_unknown_provider(self):
        self.assertEqual((await self.call('POST', body={'provider': 'amd', 'mode': 'bogus'})).status, 400)
        self.assertEqual((await self.call('POST', body={'provider': 'ghost', 'mode': 'amd'})).status, 400)
        self.assertEqual((await self.call('POST', body={'provider': 'amd', 'mode': 'custom', 'unit': 'BTC'})).status, 400)

    async def test_manual_mode_saves_and_reports_a_window(self):
        saved = await self.call('POST', body={'provider': 'all-in-one', 'mode': 'manual', 'url': '',
                                              'unit': 'cny', 'label': '', 'limit_value': 80, 'reset': 'day',
                                              'price_per_million': None, 'manual_remaining': 55.92,
                                              'models': 'deepseek-v4.1-flash', 'timezone': 'Asia/Shanghai'})
        self.assertEqual(saved.status, 200)
        refreshed = await self.call('POST', '/_audit/api/balances/refresh', {'provider': 'all-in-one', 'force': True})
        entry = (await refreshed.json())['providers'][0]
        self.assertEqual(entry['mode'], 'manual')
        self.assertAlmostEqual(entry['reading']['remaining'], 55.92)
        self.assertAlmostEqual(entry['reading']['total'], 80.0)
        self.assertEqual(entry['reading']['detail']['周期'], '每日重置')

    async def test_manual_mode_requires_a_limit(self):
        result = await self.call('POST', body={'provider': 'amd', 'mode': 'manual', 'unit': 'tokens',
                                               'limit_value': None, 'reset': 'day'})
        self.assertEqual(result.status, 400)

    async def test_unauthorized_without_token(self):
        result = await self.call(headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(result.status, 401)

    async def test_custom_url_must_stay_on_provider_host(self):
        await self.call('POST', body={'provider': 'minekuai', 'mode': 'custom', 'url': 'http://evil.example/q',
                                      'used_path': 'data', 'total_path': '', 'unit': 'usd'})
        result = await self.call('POST', '/_audit/api/balances/refresh', {'provider': 'minekuai', 'force': True})
        entry = (await result.json())['providers'][0]
        self.assertFalse(entry['reading']['ok'])
        self.assertIn('https', entry['reading']['error'])