"""Local audit proxy. No production integration or implicit upstream discovery."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from aiohttp import web

@dataclass
class Settings:
    upstream: str
    admin_token: str = field(repr=False)
    db: str = 'audit.sqlite3'
    host: str = '127.0.0.1'
    port: int = 8787
    request_cap: int = 8 * 1024 * 1024
    response_cap: int = 16 * 1024 * 1024
    retention_days: int = 7
    cleanup_interval: float = 3600
    schema_path: str = ''
    snapshot_path: str = ''
    management_key: str = field(default='', repr=False)
    config_path: str = ''
    provider_hosts: tuple = ()  # Legacy compatibility only; authenticated submissions approve provider trust.
    allow_nonloopback_bind: bool = False

    def __post_init__(self):
        import ipaddress
        if self.host != 'localhost':
            try:
                ip = ipaddress.ip_address(self.host)
                if not ip.is_loopback and not (self.allow_nonloopback_bind and self.host == '0.0.0.0'):
                    raise ValueError()
            except ValueError:
                raise ValueError('AUDIT_HOST must be loopback (or explicit 0.0.0.0 container bind)') from None
        if not 1 <= self.port <= 65535:
            raise ValueError('Invalid port')
        u = urlsplit(self.upstream)
        if u.scheme not in ('http', 'https') or not u.netloc or u.username or u.password or u.query or u.fragment:
            raise ValueError('AUDIT_UPSTREAM must be an HTTP(S) URL without credentials/query/fragment')
        if not self.admin_token or min(self.request_cap, self.response_cap, self.retention_days) <= 0 or self.cleanup_interval <= 0:
            raise ValueError('Admin token and positive capture/retention limits required')

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        allow_raw = str(e.get('AUDIT_ALLOW_NONLOOPBACK_BIND', '0')).strip().lower()
        if allow_raw not in {'0', 'false', 'no', 'off', '1', 'true', 'yes', 'on'}:
            raise ValueError('Invalid AUDIT_ALLOW_NONLOOPBACK_BIND')
        allow_nonloopback = allow_raw in {'1', 'true', 'yes', 'on'}
        return cls(upstream=e.get('AUDIT_UPSTREAM',''), admin_token=e.get('AUDIT_ADMIN_TOKEN',''),
                   db=e.get('AUDIT_DB','audit.sqlite3'), host=e.get('AUDIT_HOST','127.0.0.1'),
                   port=int(e.get('AUDIT_PORT','8787')), request_cap=int(e.get('AUDIT_REQUEST_CAP','8388608')),
                   response_cap=int(e.get('AUDIT_RESPONSE_CAP','16777216')),
                   retention_days=int(e.get('AUDIT_RETENTION_DAYS','7')), management_key=e.get('AUDIT_MANAGEMENT_KEY',''),
                   config_path=e.get('AUDIT_CONFIG_PATH',''), provider_hosts=tuple(h.strip().lower() for h in e.get('AUDIT_PROVIDER_HOSTS','').split(',') if h.strip()), schema_path=e.get('AUDIT_SCHEMA_PATH',''), snapshot_path=e.get('AUDIT_SNAPSHOT_PATH',''),
                   allow_nonloopback_bind=allow_nonloopback)



import asyncio
import contextlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar
from multidict import CIMultiDict
from yarl import URL
import quotas
from datetime import date, datetime, timezone
import re

HOP = {'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','proxy-connection'}

def parse_usage(response_body, media_type):
    """Return (prompt_tokens, completion_tokens, cached_tokens, actual_model).
    Reads OpenAI usage from a JSON body or the last SSE chunk (stream: true).
    Return None fields (not 0) when unknown so the UI shows 未知."""
    prompt = completion = cached = None
    model = None
    text = None
    try:
        text = response_body.decode('utf-8', errors='replace')
    except Exception:
        return prompt, completion, cached, model
    def consume(obj):
        nonlocal prompt, completion, cached, model
        if not isinstance(obj, dict):
            return None
        usage = obj.get('usage')
        if isinstance(usage, dict):
            p = usage.get('prompt_tokens')
            c = usage.get('completion_tokens')
            if type(p) is int: prompt = p
            if type(c) is int: completion = c
            det = usage.get('prompt_tokens_details')
            if isinstance(det, dict) and type(det.get('cached_tokens')) is int:
                cached = det['cached_tokens']
        if isinstance(obj.get('model'), str) and obj['model']:
            model = obj['model'][:256]
        return None
    lowered = (media_type or '').lower()
    if 'text/event-stream' in lowered or 'stream' in lowered:
        for line in text.splitlines():
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if not payload or payload == '[DONE]':
                continue
            try:
                consume(json.loads(payload))
            except (ValueError, UnicodeError):
                continue
    else:
        try:
            consume(json.loads(text))
        except (ValueError, UnicodeError):
            pass
    return prompt, completion, cached, model

def bearer_prefix(auth_header, limit=12):
    """Non-reversible client identifier for a request: SHA-256 fingerprint prefix.
    Never stores key material; empty when absent or not a Bearer header."""
    if not auth_header:
        return ''
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return ''
    token = parts[1].strip()
    if not token:
        return ''
    import hashlib
    return hashlib.sha256(token.encode('utf-8', 'replace')).hexdigest()[:16]

def clean_headers(headers):
    blocked = HOP | {v.strip().lower() for v in headers.get('Connection','').split(',')}
    return CIMultiDict((k,v) for k,v in headers.items() if k.lower() not in blocked | {'host'})

COLUMNS = 'id,started,ended,path,method,model,request_media_type,response_media_type,status,duration,complete,request_truncated,response_truncated,error,prompt_tokens,completion_tokens,cached_tokens,actual_model,client_key,client_pool,client_name,first_byte,stream,upstream_hint'
# 健康检查 / 能力探测等非对话请求不落库，只保留真实调用记录
SKIP_LOG_PATHS = {'/v1/models', '/api/tags', '/version', '/props', '/v1/props', '/api/show',
                  '/api/v1/models', '/favicon.ico', '/favicon.svg'}
SKIP_LOG_SUFFIXES = ('/models', '/props', '/tags', '/api/show', '/version')


def should_log(path):
    """Health checks and capability probes are not conversations: never persist them."""
    if not path or path in ('/', ''):
        return False
    if path in SKIP_LOG_PATHS:
        return False
    return not path.rstrip('/').endswith(SKIP_LOG_SUFFIXES)


def upstream_hint(headers):
    """Vendor fingerprints the upstream gateway adds (not the uni-api channel name).

    uni-api exposes no per-request channel header, but some upstreams stamp their own
    provider/model (e.g. ``x-magpie-provider``), which is the only per-request signal
    available for an alias served by several channels.
    """
    hint = {}
    for name in ('x-magpie-provider', 'x-magpie-model', 'x-new-api-version'):
        value = headers.get(name)
        if isinstance(value, str) and value.strip():
            hint[name] = value.strip()[:120]
    return hint or None


DATA_URI = re.compile(rb'data:([A-Za-z0-9.+/-]+);base64,[A-Za-z0-9+/=\\]+')
BASE64_RUN = re.compile(rb'[A-Za-z0-9+/]{400,}={0,2}')


def strip_inline_base64(body):
    """Drop inline media payloads from the stored copy (the proxy still forwards them).

    A single base64 image makes a request row megabytes wide and renders as an
    unreadable wall in the detail panel, so it is replaced by a size marker.
    """
    if not body:
        return body
    body = DATA_URI.sub(lambda m: b'data:' + m.group(1) + b';base64,[base64 ' + str(len(m.group(0))).encode() + b' bytes]', body)
    return BASE64_RUN.sub(lambda m: b'[base64 ' + str(len(m.group(0))).encode() + b' bytes]', body)

def create_app(settings):
    s = settings
    @web.middleware
    async def guard(r, handler):
        if r.path.startswith('/_audit/api'):
            if not hmac.compare_digest(r.headers.get('Authorization',''), 'Bearer '+s.admin_token):
                return web.json_response({'error':'unauthorized'},status=401)
        if r.path.startswith('/_audit/api'):
            origin = r.headers.get('Origin')
            if (origin and origin != r.scheme+'://'+r.host) or r.headers.get('Sec-Fetch-Site') == 'cross-site':
                return web.json_response({'error':'Untrusted origin'},status=403)
        response = await handler(r)
        if r.path.startswith('/_audit'):
            response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                                     'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"})
        return response
    app = web.Application(middlewares=[guard], client_max_size=65536, handler_args={'auto_decompress':False})
    state = {}

    def cleanup():
        days = state['db'].execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()[0]
        cutoff = time.time()-int(days)*86400
        with state['db']:
            cur = state['db'].execute('DELETE FROM requests WHERE started < ?', (cutoff,))
            deleted = cur.rowcount
            cur = state['db'].execute('DELETE FROM quota_snapshots WHERE julianday(checked_at) < julianday(?)',
                                      (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),))
        return deleted + cur.rowcount

    async def lifecycle(app):
        db = sqlite3.connect(s.db)
        db.row_factory = sqlite3.Row
        state['db'] = db
        import management
        state['management_db'] = management.storage(':memory:' if s.db == ':memory:' else s.db+'.management')
        if s.db != ':memory:':
            os.chmod(s.db,0o600)
        db.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, started REAL, ended REAL, path TEXT, method TEXT,
        model TEXT, request_media_type TEXT, response_media_type TEXT, status INTEGER, duration REAL,
        request_body BLOB, response_body BLOB, complete INTEGER, request_truncated INTEGER,response_truncated INTEGER,error TEXT);''')
        for column, definition in (('prompt_tokens','INTEGER'),('completion_tokens','INTEGER'),('cached_tokens','INTEGER'),
                                   ('actual_model','TEXT'),('client_key','TEXT'),('client_pool','TEXT'),('client_name','TEXT'),
                                   ('first_byte','REAL'),('stream','INTEGER'),('upstream_hint','TEXT')):
            if column not in {row[1] for row in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN '+column+' '+definition)
        db.commit()
        db.execute("INSERT OR IGNORE INTO settings VALUES('retention_days',?)",(s.retention_days,))
        db.commit()
        quotas.initialize(db)
        cleanup()
        state['session'] = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False, timeout=ClientTimeout(total=None, sock_connect=15, sock_read=300), trust_env=False)
        async def periodic():
            while True:
                await asyncio.sleep(s.cleanup_interval)
                cleanup()
        task = asyncio.create_task(periodic())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await state['session'].close()
            db.close()
            state['management_db'].close()
    app.cleanup_ctx.append(lifecycle)

    async def proxy(r):
        started = time.time()
        tick = time.monotonic()
        req, resp = bytearray(), bytearray()
        hint_headers = None
        req_trunc = resp_trunc = finished = False
        status = None
        media = model = error = None
        out = None
        client_key = bearer_prefix(r.headers.get('Authorization'))
        client_pool = None
        client_name = None
        first_byte = None
        stream = 0
        try:
            async for chunk in r.content.iter_chunked(65536):
                room = s.request_cap-len(req)
                req.extend(chunk[:room])
                if len(chunk)>room:
                    req_trunc = True
                    error = 'request_too_large'
                    status = 413
                    return web.Response(status=413,text='Request exceeds configured capture cap')
            try:
                parsed = json.loads(req)
                if isinstance(parsed,dict) and isinstance(parsed.get('model'),str):
                    model = parsed['model'][:256]
                if isinstance(parsed,dict) and parsed.get('stream') is True:
                    stream = 1
            except (ValueError,UnicodeError):
                pass
            headers = clean_headers(r.headers)
            headers.popall('Content-Length',None)
            async with state['session'].request(r.method,URL(s.upstream.rstrip('/')+r.raw_path,encoded=True),
                         headers=headers,data=bytes(req),allow_redirects=False,skip_auto_headers={'Content-Type','Accept-Encoding','User-Agent'}) as upstream:
                status = upstream.status
                media = upstream.headers.get('Content-Type')
                if media and 'text/event-stream' in media.lower():
                    stream = 1
                hint_headers = upstream_hint(upstream.headers)
                out = web.StreamResponse(status=status,headers=clean_headers(upstream.headers))
                await out.prepare(r)
                async for chunk in upstream.content.iter_chunked(65536):
                    if first_byte is None:
                        first_byte = time.monotonic()-tick
                    room = s.response_cap-len(resp)
                    resp.extend(chunk[:room])
                    if len(chunk)>room:
                        resp_trunc = True
                    try:
                        await out.write(chunk)
                    except (ConnectionError,RuntimeError):
                        error = 'client_disconnect'
                        raise
                await out.write_eof()
                finished = True
                return out
        except asyncio.CancelledError:
            error = 'client_disconnect' if r.transport is None or r.transport.is_closing() else 'cancelled'
            raise
        except Exception:
            error = error or ('client_disconnect' if r.transport is None or r.transport.is_closing() else 'upstream_error')
            if out is not None and out.prepared:
                if r.transport:
                    r.transport.close()
                return out
            status = 502
            return web.Response(status=502,text='Upstream unavailable')
        finally:
            ended = time.time()
            duration = time.monotonic()-tick
            if should_log(r.path):
                try:
                    prompt, completion, cached, actual_model = parse_usage(bytes(resp), media)
                except Exception:
                    prompt = completion = cached = actual_model = None
                # Attribute the request to a client pool / name from the management index.
                # Read from the separate management DB first; never nest transactions on state['db'].
                match = None
                if client_key:
                    try:
                        mdb = state.get('management_db')
                        match = mdb.execute(
                            'SELECT pool, client_name FROM client_keys WHERE prefix=? LIMIT 1', (client_key,)).fetchone() if mdb else None
                    except Exception:
                        match = None
                try:
                    with state['db'] as db:
                        row = db.execute(
                            '''INSERT INTO requests(started,ended,path,method,model,request_media_type,
                        response_media_type,status,duration,request_body,response_body,complete,request_truncated,response_truncated,error,
                        prompt_tokens,completion_tokens,cached_tokens,actual_model,client_key,client_pool,client_name,first_byte,stream,upstream_hint)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (started,ended,r.path,r.method,model,r.headers.get('Content-Type'),media,status,duration,
                             strip_inline_base64(bytes(req)),strip_inline_base64(bytes(resp)),int(finished and not req_trunc and not resp_trunc),int(req_trunc),int(resp_trunc),error,
                             prompt,completion,cached,actual_model,client_key,
                             match[0] if match and match[0] else None,
                             match[1] if match and match[1] else None,
                             first_byte,stream,
                             json.dumps(hint_headers,ensure_ascii=False) if hint_headers else None))
                        request_id = row.lastrowid
                except Exception:
                    request_id = None

    async def requests(r):
        clauses, args = [], []
        for param,col in [('search','path'),('model','model'),('method','method'),('status','status')]:
            if r.query.get(param):
                clauses.append(col+' LIKE ?')
                args.append('%'+r.query[param][:256]+'%')
        try:
            offset = max(0,int(r.query.get('offset','0')))
        except ValueError:
            raise web.HTTPBadRequest(text='Invalid offset')
        sql = 'SELECT '+COLUMNS+' FROM requests'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY started DESC,id DESC LIMIT 100 OFFSET ?'
        return web.json_response([dict(row) for row in state['db'].execute(sql,(*args,offset))])

    async def detail(r):
        row = state['db'].execute('SELECT * FROM requests WHERE id=?',(r.match_info['id'],)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        if r.method == 'DELETE':
            with state['db']:
                state['db'].execute('DELETE FROM requests WHERE id=?',(row['id'],))
            return web.json_response({'deleted':1})
        d = dict(row)
        import base64
        for key in ('request_body','response_body'):
            raw = d[key]
            d[key+'_base64'] = base64.b64encode(raw).decode('ascii')
            d[key] = raw.decode('utf-8',errors='replace')
        return web.json_response(d)

    async def retention(r):
        if r.method not in ('GET','PUT'):
            raise web.HTTPMethodNotAllowed(r.method,['GET','PUT'])
        if r.method == 'PUT':
            try:
                days = (await r.json())['retention_days']
                if type(days) is not int or not 1 <= days <= 3650:
                    raise ValueError()
            except (ValueError,KeyError,TypeError):
                raise web.HTTPBadRequest(text='retention_days must be an integer from 1 to 3650')
            with state['db']:
                state['db'].execute("UPDATE settings SET value=? WHERE key='retention_days'",(days,))
            cleanup()
        return web.json_response({'retention_days':state['db'].execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()[0]})

    async def setup(r):
        # This route never uses the proxy, upstream session, or audit database.
        path = Path(s.config_path) if s.config_path else None
        exists = path is not None and os.path.lexists(path)
        if r.method == 'GET':
            return web.json_response({'configured': exists})
        if exists:
            return web.json_response({'error': 'Already configured'}, status=409)
        if path is None or not s.management_key.strip():
            return web.json_response({'error': 'Setup unavailable'}, status=503)
        try:
            body = await r.json()
            if not isinstance(body, dict) or set(body) != {'api', 'provider', 'base_url', 'models'}:
                raise ValueError()
            key = body['api']
            if (not isinstance(key, str) or not key.strip() or len(key) > 16384
                    or any(ord(c) < 32 for c in key)):
                raise ValueError()
            import management
            provider = management.name(body['provider'])
            url = await management.public_url(body['base_url'])
            models = body['models']
            if not isinstance(models, list) or not 1 <= len(models) <= 100:
                raise ValueError()
            import re
            aliases = []
            for model in models:
                values = [model] if isinstance(model, str) else list(model.keys()) + list(model.values()) if isinstance(model, dict) and len(model) == 1 else []
                if not values or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', v) or v in ('all', 'default') for v in values):
                    raise ValueError()
                aliases.append(model if isinstance(model, str) else next(iter(model.values())))
            if len(set(aliases)) != len(aliases) or key in (s.management_key, s.admin_token):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, OSError, TimeoutError):
            return web.json_response({'error': 'Invalid setup fields'}, status=400)
        import secrets
        import tempfile
        client_key = secrets.token_urlsafe(32)
        provider_config = {'provider': provider, 'base_url': url, 'api': key, 'model': models}
        config = {'providers': [provider_config], 'api_keys': [
            {'api': s.management_key, 'role': 'admin', 'model': ['all']},
            {'api': client_key, 'model': [provider + '/' + alias for alias in aliases]}]}
        temporary = None
        try:
            # Same-directory hard link publishes a complete 0600 file atomically;
            # unlike replace(), link() cannot overwrite an existing destination.
            fd, temporary = tempfile.mkstemp(prefix='.audit-setup-', dir=path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as sink:
                os.fchmod(sink.fileno(), 0o600)
                json.dump(config, sink, ensure_ascii=True)
                sink.write('\n')
                sink.flush()
                os.fsync(sink.fileno())
            os.link(temporary, path)
        except FileExistsError:
            return web.json_response({'error': 'Already configured'}, status=409)
        except OSError:
            return web.json_response({'error': 'Setup unavailable'}, status=503)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        return web.json_response({'provider': provider, 'client_api_key': client_key}, status=201)

    async def config(r):
        if r.method != 'GET':
            raise web.HTTPMethodNotAllowed(r.method,['GET'])
        result = {'read_only':True,'limitation':'Native provider/model edits and authorization pools are available through /_audit/api/manage; this endpoint exposes structure only.', 'schema':None,'snapshot':None}
        def shape(value):
            if isinstance(value,dict):
                # Even mapping keys may contain credential values. Expose structure only.
                return {'object_fields':[shape(v) for v in value.values()]}
            if isinstance(value,list):
                return [shape(v) for v in value]
            return '<redacted>'
        for name,path in [('schema',s.schema_path),('snapshot',s.snapshot_path)]:
            if not path or not s.management_key:
                continue
            if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path or '..' in path or any(c in path for c in '\r\n'):
                result[name] = {'error':'invalid configured path'}
                continue
            try:
                async with state['session'].get(s.upstream.rstrip('/')+path,headers={'Authorization':'Bearer '+s.management_key},allow_redirects=False) as upstream:
                    data = bytearray()
                    async for chunk in upstream.content.iter_chunked(65536):
                        data.extend(chunk[:s.response_cap+1-len(data)])
                        if len(data)>s.response_cap:
                            break
                    if upstream.status != 200 or len(data)>s.response_cap or not upstream.content.at_eof():
                        raise ValueError()
                    result[name] = shape(json.loads(data))
            except Exception:
                result[name] = {'error':'snapshot unavailable'}
        return web.json_response(result)

    def quota_view(provider):
        row = state['db'].execute('SELECT value FROM quota_policies WHERE provider=?', (provider,)).fetchone()
        policy = json.loads(row[0]) if row else None
        row = state['db'].execute('SELECT * FROM quota_snapshots WHERE provider=? ORDER BY id DESC LIMIT 1', (provider,)).fetchone()
        result = dict(row) if row else {'provider': provider, 'status': 'unknown', 'source': None, 'checked_at': None, 'value': {}}
        if row:
            result['value'] = json.loads(result['value'])
        value = result['value']
        balance = value.get('balance') if isinstance(value, dict) else None
        keys = value.get('keys', [])
        keys = [key for key in keys if isinstance(key, dict)] if isinstance(keys, list) else []
        fields = ('position', 'status', 'kind', 'amount', 'currency', 'unlimited', 'windows', 'checked_at')
        balances = [{field: key[field] for field in fields if field in key} for key in keys]
        available = result['status'] == 'ok' and (
            any(key.get('status') == 'ok' and (key.get('amount') is not None or
                key.get('unlimited') is True or bool(key.get('windows'))) for key in keys)
            or (not keys and isinstance(balance, dict) and bool(balance)))
        daily_usage = {'value': None, 'source': None}
        if (result.get('start_date') and result.get('start_date') == result.get('end_date')
                and value.get('actual_cost_source') and value.get('actual_cost_usd') is not None):
            daily_usage = {'value': value['actual_cost_usd'], 'unit': 'USD',
                           'source': value['actual_cost_source'], 'samples': value.get('actual_cost_samples')}
        result.update(policy=policy, policy_source='configured_policy' if policy else None,
                      balances=balances, balance_available=available,
                      daily_usage=daily_usage,
                      next_reset=quotas.next_reset(value, policy),
                      stale=not result['checked_at'] or (datetime.now(timezone.utc)-datetime.fromisoformat(result['checked_at'])).total_seconds() >= 300)
        # Preserve upstream windows verbatim; a 1d window alone is not a daily usage contract.
        return quotas.sanitize(result, secrets=(s.management_key, s.admin_token))

    def quota_provider(r):
        provider = r.match_info['provider']
        if not provider.strip() or len(provider) > 200 or any(ord(c) < 32 for c in provider) or any(k and k in provider for k in (s.management_key, s.admin_token)):
            raise web.HTTPBadRequest(text='Invalid provider name')
        return provider

    async def quota_list(r):
        rows = state['db'].execute('SELECT provider FROM quota_policies UNION SELECT provider FROM quota_snapshots ORDER BY provider')
        return web.json_response([quota_view(row[0]) for row in rows])

    async def quota_policy(r):
        provider = quota_provider(r)
        try:
            policy = quotas.validate_policy(await r.json())
            if quotas.sanitize(policy, secrets=(s.management_key, s.admin_token)) != policy:
                raise ValueError()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='Invalid quota policy') from None
        with state['db']:
            state['db'].execute('INSERT INTO quota_policies VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET value=excluded.value', (provider, json.dumps(policy)))
        return web.json_response(quota_view(provider))

    async def quota_refresh(r):
        provider = quota_provider(r)
        try:
            body = await r.json()
            if not isinstance(body, dict) or set(body) != {'start_date', 'end_date'}:
                raise ValueError()
            for key in ('start_date', 'end_date'):
                if not isinstance(body[key], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', body[key]):
                    raise ValueError()
                date.fromisoformat(body[key])
            if body['start_date'] > body['end_date']:
                raise ValueError()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='Expected ordered YYYY-MM-DD date range') from None
        status, source, value = 'missing_management_key', None, {}
        if s.management_key:
            status = 'upstream_error'
            try:
                async with state['session'].get(s.upstream.rstrip('/')+'/v1/channel-balances',
                        params={'provider': provider, **body}, headers={'Authorization': 'Bearer '+s.management_key},
                        allow_redirects=False, timeout=ClientTimeout(total=20, sock_connect=5, sock_read=10)) as upstream:
                    cap = min(s.response_cap, 1024*1024)
                    data = bytearray()
                    async for chunk in upstream.content.iter_chunked(65536):
                        data.extend(chunk[:cap+1-len(data)])
                        if len(data) > cap:
                            break
                    if len(data) > cap:
                        status = 'body_too_large'
                    elif upstream.status != 200:
                        status = 'http_error'
                    else:
                        status = 'malformed_json'
                        parsed = json.loads(data)
                        if isinstance(parsed, dict):
                            value = quotas.sanitize(parsed, secrets=(s.management_key, s.admin_token))
                            source = value.get('source') if isinstance(value.get('source'), str) else None
                            keys = value.get('keys')
                            if isinstance(keys, list):
                                keys = [key for key in keys if isinstance(key, dict)]
                                if value.get('status') == 'unsupported':
                                    status = 'unsupported'
                                elif any(key.get('status') == 'ok' for key in keys):
                                    status = 'ok'
                                elif keys and all(key.get('status') == 'unsupported' for key in keys):
                                    status = 'unsupported'
                                else:
                                    status = 'partial' if value.get('status') == 'partial' else 'unknown'
                                sources = list(dict.fromkeys(key['source'] for key in keys
                                    if key.get('status') == 'ok' and isinstance(key.get('source'), str)))
                                source = sources[0] if len(sources) == 1 else source
                            else:
                                status = 'unsupported' if value.get('status') == 'unsupported' else ('ok' if value.get('status') == 'ok' else 'unknown')
            except (ValueError, UnicodeError):
                status = 'malformed_json'
            except Exception:
                status = 'upstream_error'
        quotas.save_snapshot(state['db'], provider, body['start_date'], body['end_date'], status, source, value, secrets=(s.management_key, s.admin_token))
        return web.json_response(quota_view(provider))

    async def ui(r):
        name = 'index.html' if r.path in ('/','/_audit','/_audit/') else ('console.css' if r.path.endswith('/console.css') else 'app.js')
        return web.Response(text=Path(__file__).with_name(name).read_text(),content_type='text/html' if name.endswith('html') else ('text/css' if name.endswith('css') else 'application/javascript'))
    async def usage(r):
        try:
            days = int(r.query.get('days', '7'))
        except ValueError:
            raise web.HTTPBadRequest(text='Invalid days')
        if not 1 <= days <= 90:
            raise web.HTTPBadRequest(text='Invalid days')
        cutoff = time.time() - days * 86400
        daily = {}
        for row in state['db'].execute(
                '''SELECT strftime('%Y-%m-%d', started, 'unixepoch') AS day,
                          COUNT(*) AS requests,
                          SUM(COALESCE(prompt_tokens,0)) AS prompt,
                          SUM(COALESCE(completion_tokens,0)) AS completion,
                          SUM(COALESCE(cached_tokens,0)) AS cached
                   FROM requests WHERE started >= ? GROUP BY day ORDER BY day''', (cutoff,)):
            daily[row['day']] = {'day': row['day'], 'requests': row['requests'],
                                 'prompt': row['prompt'], 'completion': row['completion'],
                                 'cached': row['cached'],
                                 'total': row['prompt'] + row['completion']}
        models = [dict(row) for row in state['db'].execute(
            '''SELECT COALESCE(actual_model, model, '未知') AS model,
                      COUNT(*) AS requests,
                      SUM(COALESCE(prompt_tokens,0)) AS prompt,
                      SUM(COALESCE(completion_tokens,0)) AS completion,
                      SUM(COALESCE(cached_tokens,0)) AS cached
               FROM requests WHERE started >= ? GROUP BY model ORDER BY requests DESC LIMIT 20''', (cutoff,))]
        totals = {'requests': 0, 'prompt': 0, 'completion': 0, 'cached': 0}
        for item in daily.values():
            totals['requests'] += item['requests']
            totals['prompt'] += item['prompt']
            totals['completion'] += item['completion']
            totals['cached'] += item['cached']
        return web.json_response({'daily': list(daily.values()), 'models': models, 'totals': totals})

    async def health(r):
        return web.json_response({'status':'ok'})
    import management
    management.install(app, s, state)
    import usage as provider_usage
    provider_usage.install(app, s, state)
    async def favicon(r):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" data-brand="double-fork-tree"><rect width="64" height="64" rx="16" fill="#14251f"/><path d="M32 54V35M32 35 18 21M32 35l14-14M18 21V10M18 21 9 12M18 21l9-9M46 21V10M46 21l-9-9M46 21l9-9" fill="none" stroke="#63d5b4" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        return web.Response(text=svg, content_type='image/svg+xml', headers={'Cache-Control':'public, max-age=86400'})

    app.router.add_get('/',ui)
    app.router.add_get('/favicon.svg',favicon)
    app.router.add_get('/favicon.ico',favicon)
    app.router.add_get('/health',health)
    app.router.add_get('/_audit',ui)
    app.router.add_get('/_audit/',ui)
    app.router.add_get('/_audit/app.js',ui)
    app.router.add_get('/_audit/console.css',ui)
    import model_discovery
    model_discovery.install(app)
    app.router.add_get('/_audit/api/setup', setup)
    app.router.add_post('/_audit/api/setup', setup)
    app.router.add_get('/_audit/api/requests',requests)
    app.router.add_get('/_audit/api/requests/{id}',detail)
    app.router.add_delete('/_audit/api/requests/{id}',detail)
    app.router.add_route('*','/_audit/api/settings',retention)
    async def cleanup_handler(r):
        return web.json_response({'deleted':cleanup()})
    app.router.add_post('/_audit/api/cleanup',cleanup_handler)
    app.router.add_route('*','/_audit/api/config',config)
    app.router.add_get('/_audit/api/usage', usage)
    app.router.add_get('/_audit/api/quotas', quota_list)
    app.router.add_put('/_audit/api/quotas/{provider}', quota_policy)
    app.router.add_post('/_audit/api/quotas/{provider}/refresh', quota_refresh)
    async def unknown_admin(r):
        raise web.HTTPNotFound()
    app.router.add_route('*','/_audit/{path:.*}',unknown_admin)
    app.router.add_route('*','/{path:.*}',proxy)
    return app

if __name__ == '__main__':
    try:
        settings = Settings.from_env()
    except (ValueError,TypeError):
        raise SystemExit('Invalid audit configuration: required upstream/token and valid positive limits needed')
    web.run_app(create_app(settings),host=settings.host,port=settings.port,access_log=None,print=None)
