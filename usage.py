"""Per-provider upstream quota/balance readers for the audit console.

Every provider can be assigned one quota strategy ("mode"):

  none            - no reading (default until configured)
  openrouter      - GET {base}/credits (total_credits/total_usage) +
                    GET {base}/key (free-model daily quota)
  openai_billing  - OpenAI-style GET {base}/dashboard/billing/{subscription,usage}
                    (hard-limit + cumulative usage in cents)
  amd             - GET {base}/key + GET {base}/usage (AMD Radeon daily USD budget)
  custom          - operator-supplied https URL (or path relative to the provider
                    base) plus JSON paths for used/total/remaining

Readings are normalized to used/total/remaining/percent/unit/label plus a small
numeric detail dict. Provider credentials never leave the server: they are read
from the native config through the management credential at request time and are
never stored, echoed, or logged here. All fetches are non-redirecting, capped in
size, and every string kept in a reading is scrubbed of key-shaped substrings.
"""

import json
import re
import time

from aiohttp import ClientTimeout
from aiohttp import web

MODE_NONE = 'none'
MODES = ('none', 'openrouter', 'openai_billing', 'amd', 'custom')
UNITS = ('usd', 'cny', 'credits', 'count', 'percent', 'usd_cents')
TTL_SECONDS = 300
CAP = 256 * 1024

_HOST_DEFAULTS = (
    ('openrouter.ai', 'openrouter'),
    ('developer.amd.com', 'amd'),
    ('freesharenow', 'openai_billing'),
)


def default_mode(base_url):
    match = (base_url or '').lower()
    for fragment, mode in _HOST_DEFAULTS:
        if fragment in match:
            return mode
    return MODE_NONE


def num(value):
    """Coerce numeric JSON to float; tolerate currency/thousand separators."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(',', '').replace('$', '').replace('¥', '').replace('%', '')
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


_PATH_SEGMENT = re.compile(r'([^\[\].]+)(?:\[(\d+)\])?')


def parse_path(data, path):
    """Read 'a.b[2].c' style paths from parsed JSON. Returns None on any miss."""
    if not path or data is None:
        return None
    try:
        current = data
        for raw in path.split('.'):
            if not raw:
                return None
            match = _PATH_SEGMENT.fullmatch(raw)
            if not match:
                return None
            key, index = match.groups()
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
            else:
                return None
            if index is not None:
                index = int(index)
                if not isinstance(current, list) or index >= len(current):
                    return None
                current = current[index]
        return current
    except (TypeError, KeyError, IndexError, ValueError):
        return None


_KEY_SHAPE = re.compile(r'sk-[A-Za-z0-9_\-]{4,}')


def scrub(value):
    return _KEY_SHAPE.sub('[REDACTED]', str(value))[:240]


def _percent(total, remaining):
    if isinstance(total, (int, float)) and total > 0 and isinstance(remaining, (int, float)):
        return max(0.0, min(100.0, remaining / total * 100.0))
    return None


def _reading(provider, mode, used, total, remaining, unit, label, detail=None, error=''):
    return {
        'provider': provider, 'mode': mode, 'ok': not error,
        'used': used, 'total': total, 'remaining': remaining,
        'percent': None if error else _percent(total, remaining),
        'unit': unit, 'label': label,
        'detail': {str(k): v for k, v in (detail or {}).items()},
        'error': scrub(error)[:200],
    }


def build_openrouter(provider, credits_payload, key_payload):
    """OpenRouter: {base}/credits + {base}/key (free-model daily quota)."""
    credits = (credits_payload or {}).get('data')
    total = num((credits or {}).get('total_credits'))
    used = num((credits or {}).get('total_usage'))
    remaining = total - used if total is not None and used is not None else None
    detail = {}
    free = (key_payload or {}).get('data', {}).get('free_model_daily_requests') or {}
    if isinstance(free, dict):
        f_used, f_limit, f_remaining = num(free.get('used')), num(free.get('limit')), num(free.get('remaining'))
        if f_limit:
            detail['免费模型今日'] = '%.0f/%.0f（剩 %.0f）' % (f_used or 0, f_limit, f_remaining if f_remaining is not None else max(0, f_limit - (f_used or 0)))
    detail['免费档'] = bool((key_payload or {}).get('data', {}).get('is_free_tier'))
    return _reading(provider, 'openrouter', used, total, remaining, 'usd', '账户余额', detail)


def build_openai_billing(provider, subscription, usage):
    """OpenAI-style billing: hard limit (USD) + cumulative usage (cents)."""
    total = num((subscription or {}).get('hard_limit_usd'))
    cents = num((usage or {}).get('total_usage'))
    used = cents / 100.0 if cents is not None else None
    unlimited = total is not None and total >= 1_000_000
    if unlimited:
        label = '累计用量（不限额度）'
        remaining = None
    else:
        label = '账户余额'
        remaining = total - used if total is not None and used is not None else None
    return _reading(provider, 'openai_billing', used, total, remaining, 'usd', label, {'累计消费USD': used})


def build_amd(provider, key_payload, usage):
    """AMD Radeon developer cloud: /key usage + /usage daily budget."""
    usage = usage or {}
    total = num(usage.get('daily_cost_limit_usd'))
    used = num(usage.get('daily_cost_used_usd'))
    remaining = num(usage.get('daily_cost_remaining_usd'))
    detail = {}
    if isinstance(usage.get('all_time'), dict):
        all_time = usage['all_time']
        detail['累计请求'] = num(all_time.get('requests'))
        detail['错误'] = num(all_time.get('errors'))
        detail['累计tokens'] = num(all_time.get('total_tokens'))
    detail['RPM限制'] = num(usage.get('rpm_limit'))
    for key in ('daily_reset_timezone', 'period_duration_unit'):
        if usage.get(key):
            detail[key] = scrub(usage.get(key))[:40]
    return _reading(provider, 'amd', used, total, remaining, 'usd', '今日预算', detail)


def build_custom(provider, payload, config):
    used = num(parse_path(payload, config.get('used_path', '')))
    total = num(parse_path(payload, config.get('total_path', '')))
    remaining = num(parse_path(payload, config.get('remaining_path', '')))
    if remaining is None and total is not None and used is not None:
        remaining = total - used
    unit = config.get('unit', 'usd')
    if unit == 'usd_cents':
        used = used / 100.0 if used is not None else None
        total = total / 100.0 if total is not None else None
        remaining = remaining / 100.0 if remaining is not None else None
        unit = 'usd'
    label = config.get('label', '') or '自定义余量'
    return _reading(provider, 'custom', used, total, remaining, unit, label)


def _decode(raw):
    """The shared session disables auto-decompress; honour gzip manually."""
    body = bytes(raw)
    if body[:2] == b'\x1f\x8b':
        import gzip
        body = gzip.decompress(body)
    return body


async def _fetch_json(session, url, key, cap=CAP):
    async with session.get(
            url,
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                     'Accept-Encoding': 'identity'},
            allow_redirects=False,
            timeout=ClientTimeout(total=20)) as response:
        raw = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            raw.extend(chunk)
            if len(raw) > cap:
                raise ValueError('response too large')
        if response.status != 200:
            raise ValueError('HTTP %d' % response.status)
        return json.loads(_decode(raw))


def _endpoint(base_url, path):
    return (base_url or '').rstrip('/') + '/' + path.lstrip('/')


def _custom_url(base_url, configured):
    url = (configured or '').strip()
    if not url:
        raise ValueError('custom URL required')
    if url.startswith('/'):
        root = (base_url or '').rstrip('/')
        if root.endswith('/v1'):
            root = root[:-3]
        return root.rstrip('/') + '/' + url.lstrip('/')
    if url.startswith('https://') and ' ' not in url:
        return url
    # Custom URLs must match the provider host: avoids turning the console into a
    # credential-bearing proxy for arbitrary destinations.
    from urllib.parse import urlsplit
    base_host = urlsplit(base_url or '').netloc.lower()
    target_host = urlsplit(url).netloc.lower()
    if url.startswith('http://') or target_host != base_host:
        raise ValueError('custom URL must be https and on the provider host')
    return url


async def _read_provider(session, provider, base_url, key, config, fetch):
    mode = config.get('mode', MODE_NONE)
    if mode == MODE_NONE:
        return None
    if not key:
        return _reading(provider, mode, None, None, None, 'usd', '', error='渠道未配置凭证')
    try:
        if mode == 'openrouter':
            credits = await fetch(session, _endpoint(base_url, 'credits'), key)
            keyinfo = await fetch(session, _endpoint(base_url, 'key'), key)
            return build_openrouter(provider, credits, keyinfo)
        if mode == 'openai_billing':
            subscription = await fetch(session, _endpoint(base_url, 'dashboard/billing/subscription'), key)
            usage = await fetch(session, _endpoint(base_url, 'dashboard/billing/usage'), key)
            return build_openai_billing(provider, subscription, usage)
        if mode == 'amd':
            keyinfo = await fetch(session, _endpoint(base_url, 'key'), key)
            usage = await fetch(session, _endpoint(base_url, 'usage'), key)
            return build_amd(provider, keyinfo, usage)
        if mode == 'custom':
            payload = await fetch(session, _custom_url(base_url, config.get('url')), key)
            return build_custom(provider, payload, config)
    except Exception as error:  # per-provider resilience
        return _reading(provider, mode, None, None, None, 'usd', '', error='查询失败：' + str(error))
    return _reading(provider, mode, None, None, None, 'usd', '', error='不支持的余量方式')


def install(app, settings, state):
    def db():
        connection = state['management_db']
        connection.execute('CREATE TABLE IF NOT EXISTS provider_usage(provider TEXT PRIMARY KEY, mode TEXT NOT NULL, url TEXT, used_path TEXT, total_path TEXT, remaining_path TEXT, unit TEXT, label TEXT, updated_at TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS usage_readings(provider TEXT PRIMARY KEY, checked_at REAL NOT NULL, payload TEXT NOT NULL)')
        return connection

    def load_config(provider):
        row = db().execute('SELECT mode,url,used_path,total_path,remaining_path,unit,label FROM provider_usage WHERE provider=?', (provider,)).fetchone()
        if not row:
            return {'mode': None, 'url': '', 'used_path': '', 'total_path': '', 'remaining_path': '', 'unit': 'usd', 'label': ''}
        return {'mode': row[0], 'url': row[1] or '', 'used_path': row[2] or '', 'total_path': row[3] or '',
                'remaining_path': row[4] or '', 'unit': row[5] or 'usd', 'label': row[6] or ''}

    cache = {'at': 0.0, 'entries': []}

    async def native_entries():
        """Providers straight from the native config; keys stay server-side."""
        if time.time() - cache['at'] < 30 and cache['entries']:
            return list(cache['entries'])
        session = state.get('session')
        if session is None or not settings.management_key:
            raise web.HTTPServiceUnavailable(text='native configuration unavailable')
        try:
            async with session.get(
                    settings.upstream.rstrip('/') + '/v1/api_config',
                    headers={'Authorization': 'Bearer ' + settings.management_key,
                             'Accept-Encoding': 'identity'},
                    allow_redirects=False,
                    timeout=ClientTimeout(total=20)) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > CAP:
                        raise ValueError('config too large')
                if response.status != 200:
                    raise ValueError('HTTP %d' % response.status)
                config = json.loads(_decode(raw)).get('api_config')
        except Exception:
            raise web.HTTPServiceUnavailable(text='native configuration unavailable') from None
        providers = config.get('providers') if isinstance(config, dict) else None
        if not isinstance(providers, list):
            raise web.HTTPServiceUnavailable(text='native configuration unavailable')
        entries = []
        for item in providers:
            if not isinstance(item, dict) or not item.get('provider'):
                continue
            api = item.get('api')
            key = api if isinstance(api, str) else (api[0] if isinstance(api, list) and api and isinstance(api[0], str) else '')
            entries.append({'provider': item['provider'], 'base_url': item.get('base_url') or '', 'api': key})
        cache['at'], cache['entries'] = time.time(), entries
        return list(entries)

    async def providers():
        override = state.get('usage_providers_override')
        if override is not None:
            return list(override)
        return await native_entries()

    async def fetch(session, url, key):
        return await _fetch_json(session, url, key)

    async def refresh_one(provider_entry, force=False):
        provider = provider_entry['provider']
        config = load_config(provider)
        mode = config['mode'] or default_mode(provider_entry.get('base_url', ''))
        config['mode'] = mode
        if mode == MODE_NONE:
            return {'provider': provider, 'base_url': provider_entry.get('base_url', ''), 'mode': mode,
                    'custom': config, 'reading': None, 'fetched_at': 0, 'age': 0}
        cached = None
        if not force:
            row = db().execute('SELECT checked_at,payload FROM usage_readings WHERE provider=?', (provider,)).fetchone()
            if row and time.time() - row[0] < TTL_SECONDS:
                cached = json.loads(row[1])
        if cached is None:
            session = state.get('session')
            if session is None:
                cached = _reading(provider, mode, None, None, None, 'usd', '', error='runtime session unavailable')
            else:
                fetch_fn = state.get('usage_fetch_override') or fetch
                used = await _read_provider(session, provider, provider_entry.get('base_url', ''),
                                            provider_entry.get('api', provider_entry.get('key', '')), config, fetch_fn)
                cached = used if used is None else {'provider': provider, 'mode': mode,
                                                    'ok': used.get('ok'), 'used': used.get('used'),
                                                    'total': used.get('total'), 'remaining': used.get('remaining'),
                                                    'percent': used.get('percent'), 'unit': used.get('unit'),
                                                    'label': used.get('label'), 'detail': used.get('detail'),
                                                    'error': used.get('error'), 'checked_at': time.time()}
                if cached is not None:
                    with db():
                        db().execute('INSERT OR REPLACE INTO usage_readings VALUES(?,?,?)',
                                     (provider, cached.get('checked_at', time.time()), json.dumps(cached)))
        age = int(time.time() - (cached.get('checked_at') or 0)) if cached else -1
        reading = dict(cached) if cached else None
        if reading:
            reading.pop('checked_at', None)
        return {'provider': provider, 'base_url': provider_entry.get('base_url', ''), 'mode': mode,
                'custom': config, 'reading': reading, 'fetched_at': (cached or {}).get('checked_at', 0), 'age': age}

    async def list_handler(r):
        results = []
        try:
            entries = await providers()
        except web.HTTPServiceUnavailable:
            return web.json_response({'error': '原生配置暂不可用，请稍后重试'}, status=503)
        for entry in entries:
            results.append(await refresh_one(entry))
        return web.json_response({'providers': results})

    async def save_handler(r):
        if r.method != 'POST':
            raise web.HTTPMethodNotAllowed(r.method, ['POST'])
        body = await r.json()
        if not isinstance(body, dict):
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        provider = (body.get('provider') or '').strip()
        mode = body.get('mode', MODE_NONE)
        if not provider or len(provider) > 128:
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        if mode not in MODES:
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        try:
            entries = await providers()
        except web.HTTPServiceUnavailable:
            return web.json_response({'error': '原生配置暂不可用，请稍后重试'}, status=503)
        if not any(e.get('provider') == provider for e in entries):
            return web.json_response({'error': '未知渠道'}, status=400)
        unit = body.get('unit', 'usd')
        if unit not in UNITS:
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        simple = {}
        for field in ('url', 'used_path', 'total_path', 'remaining_path', 'label'):
            value = body.get(field, '')
            simple[field] = value if isinstance(value, str) else ''
        if len(simple['url']) > 500 or any(len(simple[field]) > 200 for field in ('used_path', 'total_path', 'remaining_path')):
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        if len(simple['label']) > 60:
            return web.json_response({'error': 'Invalid fields or unsupported change'}, status=400)
        with db():
            db().execute('INSERT OR REPLACE INTO provider_usage VALUES(?,?,?,?,?,?,?,?,?)',
                         (provider, mode, simple['url'], simple['used_path'], simple['total_path'],
                          simple['remaining_path'], unit, simple['label'], time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
        with db():
            db().execute('DELETE FROM usage_readings WHERE provider=?', (provider,))
        return web.json_response({'saved': {'provider': provider, 'mode': mode}})

    async def refresh_handler(r):
        if r.method != 'POST':
            raise web.HTTPMethodNotAllowed(r.method, ['POST'])
        body = await r.json() or {}
        force = bool(body.get('force'))
        target = (body.get('provider') or '').strip()
        results = []
        try:
            entries = await providers()
        except web.HTTPServiceUnavailable:
            return web.json_response({'error': '原生配置暂不可用，请稍后重试'}, status=503)
        for entry in entries:
            if target and entry['provider'] != target:
                continue
            results.append(await refresh_one(entry, force=force))
        return web.json_response({'providers': results})

    app.router.add_get('/_audit/api/balances', list_handler)
    app.router.add_post('/_audit/api/balances', save_handler)
    app.router.add_post('/_audit/api/balances/refresh', refresh_handler)