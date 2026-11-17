"""Attribution from uni-api's request statistics (read-only SQLite)."""

import os
import sqlite3
import tempfile
import time
import unittest

import attribution


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'stats.db')
        db = sqlite3.connect(self.path)
        db.execute('''CREATE TABLE request_stats(id INTEGER PRIMARY KEY, request_id TEXT,
                      provider TEXT, model TEXT, upstream_model TEXT, prompt_tokens INTEGER,
                      completion_tokens INTEGER, timestamp TEXT)''')
        self.now = time.time()
        stamp = attribution._stamp(self.now)
        rows = [
            ('r1', 'all-in-one', 'deepseek-v4.1-flash', 120, 8, stamp),
            ('r2', 'minekuai', 'deepseek-v4.1-flash-1017', 33, 2, stamp),
            ('r3', 'amd', 'qwen-3.8-27b', 120, 8, stamp),
        ]
        db.executemany('INSERT INTO request_stats(request_id,provider,model,upstream_model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?,?)',
                       [(r[0], r[1], r[2], r[2], r[3], r[4], r[5]) for r in rows])
        db.commit()
        db.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_exact_token_match_returns_provider(self):
        self.assertEqual(attribution.lookup(self.path, 'deepseek-v4.1-flash', 120, 8, self.now), 'all-in-one')
        self.assertEqual(attribution.lookup(self.path, 'deepseek-v4.1-flash-1017', 33, 2, self.now), 'minekuai')

    def test_ambiguous_rows_are_not_guessed(self):
        db = sqlite3.connect(self.path)
        db.execute('INSERT INTO request_stats(request_id,provider,model,upstream_model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?,?)',
                   ('r4', 'minekuai', 'deepseek-v4.1-flash', 'minekuai/x', 120, 8, attribution._stamp(self.now)))
        db.commit()
        db.close()
        self.assertIsNone(attribution.lookup(self.path, 'deepseek-v4.1-flash', 120, 8, self.now))

    def test_outside_window_or_missing_tokens_returns_none(self):
        self.assertIsNone(attribution.lookup(self.path, 'deepseek-v4.1-flash', 120, 8, self.now - 3600))
        self.assertIsNone(attribution.lookup(self.path, 'deepseek-v4.1-flash', None, None, self.now))

    def test_wal_database_is_read_through_a_snapshot(self):
        wal = os.path.join(self.dir.name, 'wal.db')
        db = sqlite3.connect(wal)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE request_stats(id INTEGER PRIMARY KEY, provider TEXT, model TEXT,'
                   ' upstream_model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, timestamp TEXT)')
        db.execute('INSERT INTO request_stats(provider,model,upstream_model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?)',
                   ('amd', 'glm-5.3-flash', 'self/x', 77, 5, attribution._stamp(self.now)))
        db.commit()
        # Keep the connection open so the WAL sidecar is still live, like the real service.
        self.assertEqual(attribution.lookup(wal, 'glm-5.3-flash', 77, 5, self.now), 'amd')
        db.close()

    def test_enrich_prefers_the_closest_timestamp(self):
        db = sqlite3.connect(self.path)
        db.execute('INSERT INTO request_stats(request_id,provider,model,upstream_model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?,?)',
                   ('r9', 'minekuai', 'deepseek-v4.1-flash', 'minekuai/x', 120, 8, attribution._stamp(self.now - 5)))
        db.commit(); db.close()
        rows = [{'id': 7, 'started': self.now, 'model': 'deepseek-v4.1-flash',
                 'prompt_tokens': 120, 'completion_tokens': 8}]
        joined = attribution.enrich(self.path, rows)
        self.assertEqual(joined[7]['provider'], 'all-in-one')

    def test_enrich_leaves_unmatched_rows_alone(self):
        rows = [{'id': 8, 'started': self.now, 'model': 'nope', 'prompt_tokens': 1, 'completion_tokens': 1}]
        self.assertEqual(attribution.enrich(self.path, rows), {})

    def test_missing_database_is_safe(self):
        self.assertFalse(attribution.available('/nonexistent/stats.db'))
        self.assertIsNone(attribution.lookup('/nonexistent/stats.db', 'm', 1, 1, self.now))


if __name__ == '__main__':
    unittest.main()