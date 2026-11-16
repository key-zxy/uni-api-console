"""Explicit, bounded OpenAI catalog retrieval, independent of native bootstrap."""
import asyncio
import json
import socket
from urllib.parse import urlsplit, urlunsplit
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, web
from aiohttp.abc import AbstractResolver


def models_url(base, exact=''):
    value = exact or base
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError()
    u = urlsplit(value)
    if (u.scheme not in ('http', 'https') or not u.hostname or u.username is not None
            or u.password is not None or u.query or u.fragment
            or (u.port is not None and not 1 <= u.port <= 65535)):
        raise ValueError()
    path = u.path.rstrip('/')
    if not exact:
        for suffix in ('/chat/completions', '/responses/compact', '/responses'):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        path += '/models' if path.endswith('/v1') else '/v1/models'
    return urlunsplit((u.scheme, u.netloc, path if not exact else u.path, '', ''))


def model_ids(payload):
    if isinstance(payload,dict):
        if any(payload.get(field) not in (None,False,'') for field in ('has_more','next','next_page','next_cursor','continuation_token')):
            raise ValueError()
        links = payload.get('links')
        if links is not None and (not isinstance(links, dict) or any(links.get(k) for k in ('next', 'next_page', 'next_cursor'))):
            raise ValueError()
        if any(payload.get(field) for field in ('pagination','error')):
            raise ValueError()
        for field in ('total', 'total_count'):
            if field in payload and (type(payload[field]) is not int or payload[field] != len(payload.get('data',[]))):
                raise ValueError()
    items = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError()
    result = []
    for item in items:
        value = item.get('id') if isinstance(item, dict) else None
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        if value not in result:
            result.append(value)
    return result


class ProviderResolver(AbstractResolver):
    """Resolve administrator-selected providers, including LAN and fake-IP DNS."""
    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError()
        return [dict(hostname=host, host=a[4][0], port=port, family=a[0], proto=a[2], flags=socket.AI_NUMERICHOST) for a in addresses]

    async def close(self):
        pass


async def fetch_models(url, key):
    connector = TCPConnector(resolver=ProviderResolver(), use_dns_cache=False)
    async with ClientSession(connector=connector, cookie_jar=DummyCookieJar(), trust_env=False,
                             timeout=ClientTimeout(total=12), auto_decompress=False) as session:
        async with session.get(url, headers={'Authorization':'Bearer '+key, 'Accept':'application/json', 'Accept-Encoding':'identity'}, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('上游返回 HTTP ' + str(response.status) + '；不跟随重定向。')
            if response.headers.get('Link') or response.headers.get('Content-Range'):
                raise ValueError('无法确认完整目录。')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ValueError('模型目录超过 1 MiB 限制。')
            return model_ids(json.loads(raw))


def install(app):
    async def discover(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'base_url', 'models_url', 'api'}:
                raise ValueError()
            key = body['api']
            if not isinstance(key, str) or not key or len(key) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                return web.json_response({'error':'请输入此目标上游的 Key；已有 Provider 也需重新输入。'}, status=400)
            url = models_url(body['base_url'], body['models_url'])
        except (ValueError, TypeError):
            return web.json_response({'error':'地址无效：使用无账号、查询参数或片段的 HTTP 或 HTTPS 地址（端口 1–65535）。'}, status=400)
        try:
            async with asyncio.timeout(12):
                models = await fetch_models(url, key)
            # Do not reflect a malicious catalog containing the submitted credential.
            if any(key in model for model in models):
                raise ValueError()
            return web.json_response({'models':models})
        except Exception:
            return web.json_response({'error':'无法获取模型：请检查 Key、地址、上游状态及 OpenAI data/id 格式；支持 HTTP/HTTPS 及内网地址，不跟随重定向，限 12 秒、1 MiB、1000 个模型。可继续手动输入。'}, status=400)
    app.router.add_post('/_audit/api/models/discover', discover)
