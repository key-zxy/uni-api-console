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


def _stamp(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.UTC).strftime(STAMP_FORMAT)


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
            rows = db.execute(
                '''SELECT provider FROM request_stats
                   WHERE model = ? AND prompt_tokens = ? AND completion_tokens = ?
                     AND timestamp BETWEEN ? AND ?''',
                (model, prompt_tokens, completion_tokens, low, high)).fetchall()
    except sqlite3.Error:
        return None
    providers = {row[0] for row in rows if isinstance(row[0], str) and row[0]}
    return providers.pop() if len(providers) == 1 else None
