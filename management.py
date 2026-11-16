"""Bounded native control API adapter; never forwards model requests or retries."""
import asyncio
import copy
import json
import re
import secrets
import sqlite3
import os
import time
from model_discovery import models_url, fetch_models
from aiohttp import web, ClientTimeout


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) or 'admin' in value.lower():
        raise ValueError()
    return value


def mask(secret, short=False):
    """Displayable mask: never more than the outer characters of a credential."""
    if not isinstance(secret, str) or not secret:
        return ''
    if len(secret) <= 12:
        return '*' * 6
    if short:
        return secret[:3] + '****' + secret[-2:]
    return secret[:6] + '****' + secret[-4:]


async def public_url(value):
    """Compatibility name: syntax validation only, without DNS or host policy."""
    return models_url(value, exact=value)


# Local operator accepts unconditional expanded-config publication under a single-writer assumption.
WRITE_WARNING = (
    'Local management assumes a single native writer. Expanded environment secrets '
    'may be saved in plaintext in native configuration and version snapshots. '
    'No destination filtering or DNS rebinding protection is applied; this adapter does not constrain '
    'native redirects/proxies. Environment references are not preserved.'
)


def storage(path):
    if path != ':memory:':
        fd=os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd,0o600); os.close(fd)
    db=sqlite3.connect(path)
    db.execute('PRAGMA secure_delete=ON')
    db.executescript('CREATE TABLE IF NOT EXISTS managed_pools(name TEXT PRIMARY KEY, models TEXT NOT NULL); CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, before_config TEXT NOT NULL, after_config TEXT NOT NULL, pools TEXT NOT NULL, status TEXT NOT NULL);')
    if 'after_pools' not in {row[1] for row in db.execute('PRAGMA table_info(versions)')}:
        db.execute('ALTER TABLE versions ADD COLUMN after_pools TEXT')
        db.commit()
    return db


def install(app, settings, state):
    lock=asyncio.Lock()
    previews={}

    async def native(method, payload=None):
        try:
            async with state['session'].request(method, settings.upstream.rstrip('/')+'/v1/api_config'+('/update' if method=='POST' else ''),
                    headers={'Authorization':'Bearer '+settings.management_key}, json=payload,
                    allow_redirects=False, timeout=ClientTimeout(total=20)) as response:
                raw=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw)>settings.response_cap: raise ValueError()
                if response.status!=200: raise ValueError()
                value=json.loads(raw)
                if not isinstance(value,dict): raise ValueError()
                return value
        except Exception:
            # Never reflect native errors: compiler errors can include credentials.
            raise web.HTTPServiceUnavailable(text='Native configuration unavailable; update outcome may be uncertain. Inspect locally before retrying.') from None

    async def publish(config):
        await native('POST',{'providers':config['providers'],'api_keys':config['api_keys']})
        observed=(await native('GET')).get('api_config')
        if not isinstance(observed,dict) or any(observed.get(field)!=config[field] for field in ('providers','api_keys')):
            raise web.HTTPServiceUnavailable(text='Native configuration publication unconfirmed; roll back before further changes.')

    async def handler(r):
        if not settings.management_key:
            return web.json_response({'error':'Native management credential unavailable'},status=503)
        async with lock:
            db=state['management_db']
            db.execute('CREATE TABLE IF NOT EXISTS managed_pools(name TEXT PRIMARY KEY, models TEXT NOT NULL)')
            config=(await native('GET')).get('api_config')
            if not isinstance(config,dict) or not isinstance(config.get('providers'),list) or not isinstance(config.get('api_keys'),list):
                return web.json_response({'error':'Unsupported native configuration'},status=503)
            providers=config['providers']; keys=config['api_keys']
            if not all(isinstance(p,dict) and isinstance(p.get('provider'),str) and isinstance(p.get('model',[]),list) for p in providers) or not all(isinstance(k,dict) and isinstance(k.get('api'),str) and isinstance(k.get('role',''),str) for k in keys):
                return web.json_response({'error':'Unsupported native configuration shape'},status=503)
            if not any(k['api']==settings.management_key and 'admin' in k.get('role','').lower() and not k.get('role','').startswith('console:') for k in keys):
                return web.json_response({'error':'Explicit native admin key required'},status=503)
            pools={row[0]:json.loads(row[1]) for row in db.execute('SELECT name,models FROM managed_pools')}
            # Read-only index so the audit proxy can attribute requests to a client pool
            # without reading native credentials on every request.
            db.execute('CREATE TABLE IF NOT EXISTS client_keys(prefix TEXT PRIMARY KEY, role TEXT NOT NULL, pool TEXT, client_name TEXT)')
            if 'client_name' not in {row[1] for row in db.execute('PRAGMA table_info(client_keys)')}:
                db.execute('ALTER TABLE client_keys ADD COLUMN client_name TEXT')
                db.commit()
            with db:
                db.execute('DELETE FROM client_keys')
                import hashlib
                for k in keys:
                    role=k.get('role','')
                    match=re.fullmatch(r'console:([A-Za-z0-9_-]{1,64}):([A-Za-z0-9_-]{1,64})',role)
                    if match:
                        fingerprint=hashlib.sha256(k['api'].encode('utf-8','replace')).hexdigest()[:16]
                        db.execute('INSERT OR REPLACE INTO client_keys VALUES(?,?,?,?)',(fingerprint,role,match.group(1),match.group(2)))
            def bound_pool(members):
                if not isinstance(members, list) or not members:
                    return None
                for pool_name, pool_models in pools.items():
                    if pool_models and set(pool_models) == set(members):
                        return pool_name
                return None

            def view():
                client_rows = []
                external_rows = []
                for k in keys:
                    role = k.get('role', '')
                    match = re.fullmatch(r'console:([A-Za-z0-9_-]{1,64}):([A-Za-z0-9_-]{1,64})', role)
                    privileged = 'admin' in role.lower() or k.get('api') == settings.management_key
                    masked = mask(k.get('api', ''), short=privileged)
                    if match:
                        client_rows.append({'name': match.group(2), 'pool': match.group(1), 'masked': masked})
                    else:
                        external_rows.append({'role': role, 'label': role or '未命名', 'masked': masked,
                            'models': k.get('model', []), 'bound_pool': bound_pool(k.get('model')),
                            'admin': 'admin' in role.lower(),
                            'copyable': bool(k.get('api'))})
                return {'pool_semantics':'模型授权列表；不是统一别名自动路由',
                    'provider_trust':'Authenticated administrator submission approves the endpoint',
                    'native_writes_enabled':True, 'write_warning':WRITE_WARNING,
                    'providers':[{'provider':p.get('provider'), 'base_url':p.get('base_url',''), 'models':p.get('model',[])} for p in providers],
                    'versions':[{'id':v[0],'status':v[1]} for v in db.execute('SELECT id,status FROM versions ORDER BY id DESC')],
                    'external_clients':external_rows,
                    'pools':pools,'clients':client_rows}
            if r.method=='GET':
                hidden=[settings.admin_token,settings.management_key]+[k['api'] for k in keys]
                for p in providers:
                    credential=p.get('api',[])
                    hidden.extend([credential] if isinstance(credential,str) else credential if isinstance(credential,list) else [])
                def redact(value):
                    if isinstance(value,str):
                        return '<redacted>' if any(isinstance(secret,str) and secret and secret in value for secret in hidden) else value
                    if isinstance(value,list): return [redact(v) for v in value]
                    if isinstance(value,dict): return {redact(k):redact(v) for k,v in value.items()}
                    return value
                return web.json_response(redact(view()))
            try:
                body=await r.json()
                if not isinstance(body,dict): raise ValueError()
            except (ValueError,TypeError):
                return web.json_response({'error':'Invalid JSON object'},status=400)
            latest=db.execute('SELECT id,before_config,after_config,pools,status,after_pools FROM versions ORDER BY id DESC LIMIT 1').fetchone()
            if body == {'action':'rollback'}:
                if not latest:
                    return web.json_response({'error':'No saved version'},status=409)
                original=json.loads(latest[1]); target=json.loads(latest[2])
                if config != original and config != target:
                    return web.json_response({'error':'External changes detected; manual reconciliation required'},status=409)
                # Old journals cannot prove which local metadata was changed after publication.
                expected_pools=json.loads(latest[5]) if latest[5] is not None else json.loads(latest[3])
                if pools != expected_pools and not (latest[4]=='pending' and pools==json.loads(latest[3])):
                    return web.json_response({'error':'Pool metadata conflict; later pool changes must be reconciled before rollback'},status=409)
                with db: db.execute("UPDATE versions SET status='pending' WHERE id=?",(latest[0],))
                await publish(original)
                with db:
                    db.execute('DELETE FROM managed_pools')
                    db.executemany('INSERT INTO managed_pools VALUES(?,?)',[(k,json.dumps(v)) for k,v in json.loads(latest[3]).items()])
                    db.execute('DELETE FROM versions WHERE id=?',(latest[0],))
                return web.json_response({'rolled_back':True})
            if latest and latest[4]=='pending':
                return web.json_response({'error':'Uncertain native update; roll back before further changes'},status=409)
            before=copy.deepcopy(config)
            generated=None; pool_change=None; replacement_pools=None
            try:
                action=body['action']
                if action=='provider_discover':
                    # Read-only catalog fetch for an existing provider using the
                    # server-side stored credential. The credential itself is
                    # never returned to the browser; only the catalog is.
                    if set(body) not in ({'action','provider'},{'action','provider','models_url'}): raise ValueError()
                    provider=name(body['provider'])
                    matches=[p for p in providers if p.get('provider')==provider]
                    if len(matches)!=1: raise ValueError()
                    old=matches[0]
                    credential=old.get('api')
                    if not isinstance(credential,str) or not credential or not isinstance(old.get('engine',''),str) or old.get('engine','').lower() not in ('','openai'):
                        return web.json_response({'error':'此渠道的协议或多凭据无法用于目录获取；请手动输入 Key。'},status=409)
                    url=models_url(old.get('base_url',''), body.get('models_url',''))
                    try:
                        catalog=await fetch_models(url,credential)
                    except Exception:
                        return web.json_response({'error':'未取得完整有效的 OpenAI 模型目录；可手动输入或粘贴模型。'},status=409)
                    if any(credential in model for model in catalog):
                        return web.json_response({'error':'模型目录内容异常；请手动输入或粘贴模型。'},status=409)
                    return web.json_response({'provider':provider,'models':catalog})
                if action in ('provider_delete','provider_refresh'):
                    if set(body)!={'action','provider'}: raise ValueError()
                    provider=name(body['provider'])
                    matches=[p for p in providers if p['provider']==provider]
                    if len(matches)!=1: raise ValueError()
                    old=matches[0]
                    entries=old.get('model',[])
                    if not entries or any(not isinstance(m,str) and not (isinstance(m,dict) and len(m)==1 and all(isinstance(k,str) and isinstance(v,str) for k,v in m.items())) for m in entries):
                        return web.json_response({'error':'模型映射无法可靠同步；请先在原生配置中整理。'},status=409)
                    public_models=[m if isinstance(m,str) else next(iter(m.values())) for m in entries]
                    if len(set(public_models))!=len(public_models) or any(not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',m) or m in ('all','default') for m in public_models):
                        return web.json_response({'error':'模型别名重复或规则不受支持；无法可靠同步授权。'},status=409)
                    if action=='provider_refresh':
                        credential=old.get('api')
                        if not isinstance(credential,str) or not credential or not isinstance(old.get('engine',''),str) or old.get('engine','').lower() not in ('','openai'):
                            return web.json_response({'error':'此渠道的协议或多凭据无法安全刷新；保留全部模型。'},status=409)
                        try:
                            catalog=await fetch_models(models_url(old['base_url']),credential)
                        except Exception:
                            return web.json_response({'error':'未取得完整有效的 OpenAI 模型目录；保留全部模型。'},status=409)
                        retained=[m for m in entries if (m if isinstance(m,str) else next(iter(m))) in catalog]
                    else: retained=[]
                    removed=[m if isinstance(m,str) else next(iter(m.values())) for m in entries if m not in retained]
                    rules={provider+'/'+m for m in removed}
                    affected=[pool for pool,members in pools.items() if rules.intersection(members)]
                    updated_pools={pool:[m for m in members if m not in rules] for pool,members in pools.items()}
                    if any(not updated_pools[pool] for pool in affected):
                        return web.json_response({'error':'变更会清空授权池：'+', '.join(affected)+'。请先重新绑定模型或撤销相关 key 并调整池。'},status=409)
                    clients=[]; revoked_clients=[]; revoke_keys=[]
                    for index,k in enumerate(keys):
                        role=k.get('role','')
                        parts=role.split(':')
                        if len(parts)==3 and parts[0]=='console' and parts[1] in pools:
                            if k.get('model')!=pools[parts[1]]:
                                return web.json_response({'error':'原生 key 与授权池不一致；请先核对并同步。'},status=409)
                            if parts[1] in affected:
                                clients.append(parts[2]); k['model']=updated_pools[parts[1]][:]
                        elif removed and 'admin' in role.lower():
                            clients.append('原生管理员（保留授权）')
                        elif removed:
                            # Only exact provider-qualified rules have unambiguous scope.
                            # Native compiles missing/empty model lists as `all`: never publish [].
                            members=k.get('model')
                            if (not isinstance(members,list) or not members or
                                    any(not isinstance(m,str) or not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.:/-]+',m)
                                        or m.split('/',1)[1] in ('all','default') for m in members)):
                                return web.json_response({'error':'外部 key 授权为空或包含模糊规则，无法安全同步；请先在原生配置中核对。'},status=409)
                            if rules.intersection(members):
                                label='外部 key #'+str(index+1)
                                clients.append(label)
                                remaining=[m for m in members if m not in rules]
                                if remaining: k['model']=remaining
                                else:
                                    revoked_clients.append(label)
                                    revoke_keys.append(k)
                    for k in revoke_keys: keys.remove(k)
                    if action=='provider_delete': providers.remove(old)
                    elif not retained:
                        return web.json_response({'error':'刷新将清空渠道模型，可能触发原生自动发现；请改用删除渠道。'},status=409)
                    else: old['model']=retained
                    ticket=secrets.token_urlsafe(24)
                    previews.clear()
                    previews[ticket]=(time.monotonic()+300,before,copy.deepcopy(pools),config,updated_pools)
                    return web.json_response({'preview':ticket,'removed':removed,'pools':affected,'clients':clients,'revoked_clients':revoked_clients})
                elif action=='confirm':
                    if set(body)!={'action','preview'}: raise ValueError()
                    pending=previews.pop(body['preview'],None)
                    if not pending or pending[0]<time.monotonic() or pending[1]!=config or pending[2]!=pools:
                        return web.json_response({'error':'预览已过期或配置已变更；请重新预览。'},status=409)
                    config=copy.deepcopy(pending[3]); replacement_pools=pending[4]
                elif action=='provider':
                    if set(body)!={'action','provider','base_url','api','models'}: raise ValueError()
                    provider=name(body['provider']); models=body['models']; credential=body['api']
                    if not isinstance(models,list) or not 1<=len(models)<=100: raise ValueError()
                    old=next((p for p in providers if p.get('provider')==provider),None)
                    if not isinstance(credential,str) or len(credential)>16384 or any(ord(c)<32 for c in credential) or (not old and not credential.strip()): raise ValueError()
                    if not isinstance(body['base_url'],str): raise ValueError()
                    url=await public_url(body['base_url'])
                    if old is not None:
                        # The editor may send a bare upstream name for a model that
                        # already carries a public alias. Keep that mapping instead of
                        # silently renaming the model to the upstream name, which would
                        # break every pool rule still referencing the old alias. A bare
                        # name whose model basename matches exactly one existing alias is
                        # treated as a corrected upstream spelling of that same model.
                        old_entries=old.get('model',[])
                        source_alias={}
                        alias_names=[]
                        for m in old_entries:
                            if isinstance(m,dict) and len(m)==1:
                                src, alias = next(iter(m.items()))
                                if isinstance(src,str) and isinstance(alias,str):
                                    source_alias[src]=alias
                                    alias_names.append(alias)
                            elif isinstance(m,str):
                                alias_names.append(m)
                        alias_by_base={}
                        for alias in alias_names:
                            alias_by_base.setdefault(alias.rsplit('/',1)[-1],[]).append(alias)
                        resolved=[]
                        for m in models:
                            if isinstance(m,str) and m not in alias_names:
                                if m in source_alias and source_alias[m]!=m:
                                    resolved.append({m: source_alias[m]})
                                    continue
                                matches=alias_by_base.get(m.rsplit('/',1)[-1],[])
                                if len(matches)==1 and matches[0]!=m:
                                    resolved.append({m: matches[0]})
                                    continue
                            resolved.append(m)
                        models=resolved
                    aliases=[]
                    for m in models:
                        values=[m] if isinstance(m,str) else list(m.keys())+list(m.values()) if isinstance(m,dict) and len(m)==1 else []
                        if not values or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',v) or v in ('all','default') for v in values): raise ValueError()
                        aliases.append(m if isinstance(m,str) else next(iter(m.values())))
                    if len(set(aliases))!=len(aliases): raise ValueError()
                    if old is not None:
                        old_aliases=[m if isinstance(m,str) else next(iter(m.values()))
                                     for m in old.get('model',[]) if isinstance(m,str) or isinstance(m,dict) and len(m)==1]
                        if any(alias not in aliases for alias in old_aliases) and any(
                                'admin' not in k.get('role','').lower() and not re.fullmatch(
                                    r'console:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}',k.get('role','')) for k in keys):
                            return web.json_response({'error':'External client authorization requires reconciliation; use refresh/delete preview and confirmation before removing models'},status=409)
                    conflicts=sorted({(pool,item) for pool,members in pools.items() for item in members
                                      if item.startswith(provider+'/') and item[len(provider)+1:] not in aliases})
                    if conflicts:
                        detail='；'.join(f'{item}（池 {pool}）' for pool,item in conflicts[:6])
                        more='等' if len(conflicts)>6 else ''
                        return web.json_response({'error':f'无法保存：这些模型仍被模型池使用——{detail}{more}。请先在对应模型池中取消勾选，或保留这些模型（只写上游名会保留原公开别名；改名请写 上游模型=新别名）。'},status=409)
                    entry=copy.deepcopy(old) if old else {'provider':provider}
                    entry.update(base_url=url, model=models)
                    if credential: entry['api']=credential
                    if old is not None: providers[providers.index(old)]=entry
                    else: providers.append(entry)
                elif action=='pool':
                    if set(body)!={'action','name','models'}: raise ValueError()
                    pool=name(body['name']); models=body['models']
                    catalog=set()
                    for p in providers:
                        for m in p.get('model',[]):
                            aliases=[m] if isinstance(m,str) else list(m.values()) if isinstance(m,dict) else []
                            catalog.update(p['provider']+'/'+a for a in aliases if isinstance(a,str))
                    if not isinstance(models,list) or not 1<=len(models)<=100 or any(not isinstance(m,str) or m not in catalog or '*' in m for m in models): raise ValueError()
                    bound=[k for k in keys if k.get('role','').startswith('console:'+pool+':')]
                    for k in bound: k['model']=models[:]
                    pool_change=(pool,models)
                elif action=='client':
                    if set(body)!={'action','name','pool'}: raise ValueError()
                    client=name(body['name']); pool=name(body['pool'])
                    if pool not in pools or any(k.get('role','').endswith(':'+client) for k in keys): raise ValueError()
                    generated=secrets.token_urlsafe(32)
                    keys.append({'api':generated,'role':'console:'+pool+':'+client,'model':pools[pool], 'preferences':{'SCHEDULING_ALGORITHM':'fixed_priority'}})
                elif action=='client_update':
                    if set(body)!={'action','name','pool'}: raise ValueError()
                    client=name(body['name']); pool=name(body['pool'])
                    if pool not in pools or not isinstance(pools[pool],list) or not pools[pool]: raise ValueError()
                    matches=[k for k in keys if re.fullmatch(
                        r'console:[A-Za-z0-9_-]{1,64}:'+re.escape(client),k.get('role',''))]
                    if len(matches)!=1 or 'admin' in matches[0]['role'].lower(): raise ValueError()
                    matches[0].update(role='console:'+pool+':'+client, model=copy.deepcopy(pools[pool]))
                elif action=='revoke':
                    if set(body)!={'action','name'}: raise ValueError()
                    client=name(body['name'])
                    keys[:]=[k for k in keys if not (k.get('role','').startswith('console:') and k['role'].endswith(':'+client))]
                elif action in ('client_key','native_key'):
                    if action=='client_key':
                        if set(body)!={'action','name'}: raise ValueError()
                        client=name(body['name'])
                        matches=[k for k in keys if re.fullmatch(r'console:[A-Za-z0-9_-]{1,64}:'+re.escape(client),k.get('role',''))]
                    else:
                        if set(body)!={'action','role'}: raise ValueError()
                        outer=body['role']
                        if not isinstance(outer,str) or len(outer)>128: raise ValueError()
                        matches=[k for k in keys if k.get('role','')==outer
                                 and not re.fullmatch(r'console:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}',k.get('role',''))]
                    if len(matches)!=1 or not matches[0].get('api'):
                        raise ValueError()
                    # Operator explicitly enabled copying the native admin credential from this console.
                    return web.json_response({'key':matches[0]['api']})
                elif action in ('native_pool','native_delete'):
                    pool=None
                    if action=='native_pool':
                        if set(body)!={'action','role','pool'}: raise ValueError()
                        pool=name(body['pool'])
                        if pool not in pools or not pools[pool]: raise ValueError()
                    else:
                        if set(body)!={'action','role'}: raise ValueError()
                    outer=body['role']
                    if not isinstance(outer,str) or len(outer)>128: raise ValueError()
                    matches=[k for k in keys if k.get('role','')==outer and k.get('api')
                             and not re.fullmatch(r'console:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}',k.get('role',''))]
                    if len(matches)!=1: raise ValueError()
                    # Binding only restricts the model list; the admin role is never dropped.
                    # Deleting is refused for admin and for the management credential itself.
                    if action=='native_delete' and ('admin' in outer.lower() or matches[0]['api']==settings.management_key):
                        raise ValueError()
                    if action=='native_pool': matches[0]['model']=copy.deepcopy(pools[pool])
                    else: keys.remove(matches[0])
                else: raise ValueError()
            except (ValueError,TypeError,KeyError,OSError):
                return web.json_response({'error':'Invalid fields or unsupported change'},status=400)
            if config == before and pool_change:
                # Local metadata only: never persist expanded native credentials in a version.
                with db: db.execute('INSERT OR REPLACE INTO managed_pools VALUES(?,?)',(pool_change[0],json.dumps(pool_change[1])))
                return web.json_response({'updated':True})
            after_pools=copy.deepcopy(replacement_pools if replacement_pools is not None else pools)
            if pool_change: after_pools[pool_change[0]]=pool_change[1]
            with db:
                version=db.execute('INSERT INTO versions(before_config,after_config,pools,status,after_pools) VALUES(?,?,?,?,?)',
                    (json.dumps(before),json.dumps(config),json.dumps(pools),'pending',json.dumps(after_pools))).lastrowid
                db.execute('DELETE FROM versions WHERE id NOT IN (SELECT id FROM versions ORDER BY id DESC LIMIT 10)')
            if config!=before:
                await publish(config)
            if replacement_pools is not None:
                with db:
                    db.execute('DELETE FROM managed_pools')
                    db.executemany('INSERT INTO managed_pools VALUES(?,?)',[(k,json.dumps(v)) for k,v in replacement_pools.items()])
            if pool_change:
                with db: db.execute('INSERT OR REPLACE INTO managed_pools VALUES(?,?)',(pool_change[0],json.dumps(pool_change[1])))
            with db: db.execute("UPDATE versions SET status='applied' WHERE id=?",(version,))
            return web.json_response({'client_api_key':generated} if generated else {'updated':True},status=201 if generated else 200)
    app.router.add_get('/_audit/api/manage',handler)
    app.router.add_post('/_audit/api/manage',handler)
