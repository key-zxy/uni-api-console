"""Exact channel attribution from uni-api's own request statistics.

uni-api persists every request to SQLite (``request_stats``) with the channel that
actually served it — ``provider``, ``model``, ``prompt_tokens``,
``completion_tokens`` and a UTC ``timestamp``. The audit console only has to read
that database read-only and match the row belonging to a logged request, so a
shared public alias can be attributed without patching or forking uni-api.

Matching is deliberately strict: same model, exactly the same prompt/completion
tokens, and a bounded time window. When more than one row fits, the answer is
ambiguous and ``None`` is returned instead of guessing.
"""

import datetime
import os
import shutil
import sqlite3
import tempfile

WINDOW_BACK = 30      # seconds before the console saw the request
WINDOW_FORWARD = 300  # seconds after, for streams that finish later
STAMP_FORMAT = '%Y-%m-%d %H:%M:%S'


def available(path):
    return bool(path) and os.path.exists(path)


_SNAPSHOTS = {}


def snapshot(path):
    """Copy the live SQLite files into a writable temp dir.

    uni-api keeps its stats in WAL mode; a read-only bind mount cannot create the
    ``-shm``/``-wal`` sidecars, so opening it in place fails with "unable to open
    database file". A cached copy (keyed by mtime+size of db/wal) stays cheap.
    """
    files = [path] + [path + suffix for suffix in ('-wal', '-shm')]
    signature = tuple((os.path.getmtime(f), os.path.getsize(f)) for f in files if os.path.exists(f))
    cached = _SNAPSHOTS.get(path)
    if cached and cached[0] == signature:
        return cached[1]
    directory = cached[1] if cached else tempfile.mkdtemp(prefix='uni-api-stats-')
    target = os.path.join(directory, 'stats.db')
    for name in os.listdir(directory):
        os.unlink(os.path.join(directory, name))
    for source in files:
        if os.path.exists(source):
            shutil.copy2(source, target + source[len(path):])
    _SNAPSHOTS[path] = (signature, directory)
    return directory


def connect(path):
    return sqlite3.connect(os.path.join(snapshot(path), 'stats.db'), timeout=2.0)


def _columns(db):
    return {row[1] for row in db.execute('PRAGMA table_info(request_stats)')}


def _upstream_expr(db):
    # Older uni-api schemas store no upstream_model column.
    return 'upstream_model' if 'upstream_model' in _columns(db) else 'NULL'


def _stamp(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.UTC).strftime(STAMP_FORMAT)


def _epoch(stamp):
    try:
        return datetime.datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=datetime.UTC).timestamp()
    except (TypeError, ValueError):
        return None


def lookup(path, model, prompt_tokens, completion_tokens, started):
    """Return the provider that served this request, or None when not unambiguous."""
    if not available(path) or not model:
        return None
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    low = _stamp(started - WINDOW_BACK)
    high = _stamp(started + WINDOW_FORWARD)
    try:
        with connect(path) as db:
            upstream = _upstream_expr(db)
            rows = db.execute(
                'SELECT provider, ' + upstream + ' FROM request_stats '
                'WHERE model = ? AND prompt_tokens = ? AND completion_tokens = ? '
                'AND timestamp BETWEEN ? AND ?',
                (model, prompt_tokens, completion_tokens, low, high)).fetchall()
    except sqlite3.Error:
        return None
    providers = {row[0] for row in rows if isinstance(row[0], str) and row[0]}
    return providers.pop() if len(providers) == 1 else None


def enrich(path, rows):
    """Attach provider/upstream_model to a page of audit rows, in one query.

    ``rows`` are dicts with ``started``, ``model``, ``prompt_tokens`` and
    ``completion_tokens``. Returns {row_id: {'provider':…, 'upstream_model':…}}.
    """
    if not available(path) or not rows:
        return {}
    stamps = [row.get('started') for row in rows if isinstance(row.get('started'), (int, float))]
    if not stamps:
        return {}
    low = _stamp(min(stamps) - WINDOW_BACK)
    high = _stamp(max(stamps) + WINDOW_FORWARD)
    models = {row.get('model') for row in rows if row.get('model')}
    if not models:
        return {}
    try:
        with connect(path) as db:
            placeholders = ','.join('?' * len(models))
            upstream = _upstream_expr(db)
            found = db.execute(
                'SELECT model, prompt_tokens, completion_tokens, provider, %s, timestamp '
                'FROM request_stats WHERE timestamp BETWEEN ? AND ? AND model IN (%s)'
                % (upstream, placeholders),
                [low, high, *models]).fetchall()
    except sqlite3.Error:
        return {}
    index = {}
    for model, prompt, complete, provider, upstream, stamp in found:
        index.setdefault((model, prompt, complete), []).append((provider or '', upstream or '', stamp))
    out = {}
    for row in rows:
        hits = index.get((row.get('model'), row.get('prompt_tokens'), row.get('completion_tokens')))
        if not hits:
            continue
        started = float(row.get('started') or 0)
        providers = {hit[0] for hit in hits}
        if len(providers) == 1:
            chosen = hits[0]
        else:
            # Same model and identical token counts can repeat; the closest timestamp wins
            # and a genuine tie across providers stays unattributed instead of guessing.
            scored = [(abs(stamp - started), hit) for hit in hits
                      if (stamp := _epoch(hit[2])) is not None]
            if not scored:
                continue
            scored.sort(key=lambda item: item[0])
            if len(scored) > 1 and scored[0][0] == scored[1][0] and scored[0][1][0] != scored[1][1][0]:
                continue
            chosen = scored[0][1]
        out[row['id']] = {'provider': chosen[0] or None, 'upstream_model': chosen[1] or None}
    return out
