"""Small local quota policies and sanitized, append-only snapshots."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {'daily_limit', 'unit', 'timezone', 'reset_time'}:
        raise ValueError('Invalid policy fields')
    limit = value.get('daily_limit')
    if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0):
        raise ValueError('Invalid daily limit')
    if 'unit' in value and (not isinstance(value['unit'], str) or not value['unit'].strip() or len(value['unit']) > 64):
        raise ValueError('Invalid unit')
    if limit is not None and not value.get('unit'):
        raise ValueError('Limit requires unit')
    if ('timezone' in value) != ('reset_time' in value):
        raise ValueError('Timezone and reset time required together')
    if 'timezone' in value:
        try:
            ZoneInfo(value['timezone'])
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise ValueError('Invalid timezone') from None
        if not isinstance(value.get('reset_time'), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value['reset_time']):
            raise ValueError('Invalid reset time')
    return dict(value)


def next_reset(value, policy=None, now=None):
    now = now or datetime.now(timezone.utc)
    if isinstance(value, dict):
        raw = value.get('reset_at')
        if isinstance(raw, str):
            try:
                at = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if at.tzinfo is not None:
                    return {'at': at.isoformat(), 'source': 'provider_reported'}
            except ValueError:
                pass
    if policy and policy.get('timezone') and policy.get('reset_time'):
        zone = ZoneInfo(policy['timezone'])
        hour, minute = map(int, policy['reset_time'].split(':'))
        local = now.astimezone(zone)
        # Round-trip resolves nonexistent spring-forward wall times to the next valid time.
        for days in range(3):
            candidate = (local + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
            candidate = candidate.astimezone(timezone.utc).astimezone(zone)
            if candidate.astimezone(timezone.utc) > now:
                return {'at': candidate.isoformat(), 'source': 'configured_policy'}
    return {'at': None, 'source': None}


CREDENTIAL = re.compile(r'api[_-]?key|authorization|cookie|secret|password|access[_-]?token|refresh[_-]?token|raw[_-]?token|^token$|credential|bearer', re.I)


def sanitize(value, secrets=()):
    # Collect credential values first, so copies under innocuous fields disappear too.
    hidden = {s for s in secrets if isinstance(s, str) and s}
    def collect(v):
        if isinstance(v, dict):
            for k, item in v.items():
                if CREDENTIAL.search(str(k)):
                    def strings(x):
                        if isinstance(x, str) and x:
                            hidden.add(x)
                        elif isinstance(x, (dict, list)):
                            for child in (x.values() if isinstance(x, dict) else x):
                                strings(child)
                    strings(item)
                collect(item)
        elif isinstance(v, list):
            for item in v:
                collect(item)
    collect(value)
    def unsafe(s):
        return bool(re.search(r'bearer\s+\S+', s, re.I)) or any(secret in s for secret in hidden)
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(item) for k, item in v.items() if not CREDENTIAL.search(k) and not unsafe(k) and not (isinstance(item, str) and unsafe(item))}
        if isinstance(v, list):
            return [clean(item) for item in v if not (isinstance(item, str) and unsafe(item))]
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return None if isinstance(v, str) and unsafe(v) else v
    return clean(value)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS quota_policies(provider TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quota_snapshots(id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
    start_date TEXT, end_date TEXT, checked_at TEXT NOT NULL, status TEXT NOT NULL, source TEXT, value TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS quota_no_update BEFORE UPDATE ON quota_snapshots
    BEGIN SELECT RAISE(ABORT, 'quota snapshots are immutable'); END;''')
    db.commit()


def save_snapshot(db, provider, start_date, end_date, status, source, value, secrets=()):
    safe = sanitize({'provider': provider, 'source': source, 'value': value}, secrets)
    result = {'provider': safe.get('provider', '[redacted]'), 'start_date': start_date, 'end_date': end_date,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'status': status,
              'source': safe.get('source'), 'value': safe.get('value', {})}
    with db:
        cur = db.execute('INSERT INTO quota_snapshots(provider,start_date,end_date,checked_at,status,source,value) VALUES(?,?,?,?,?,?,?)',
                         (*[result[k] for k in ('provider','start_date','end_date','checked_at','status','source')], json.dumps(result['value'], allow_nan=False)))
    result['id'] = cur.lastrowid
    return result
