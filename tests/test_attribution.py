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
                      provider TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
                      timestamp TEXT)''')
        self.now = time.time()
        stamp = attribution._stamp(self.now)
        rows = [
            ('r1', 'all-in-one', 'deepseek-v4.1-flash', 120, 8, stamp),
            ('r2', 'minekuai', 'deepseek-v4.1-flash-1017', 33, 2, stamp),
            ('r3', 'amd', 'qwen-3.8-27b', 120, 8, stamp),
        ]
        db.executemany('INSERT INTO request_stats(request_id,provider,model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?)', rows)
        db.commit()
        db.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_exact_token_match_returns_provider(self):
        self.assertEqual(attribution.lookup(self.path, 'deepseek-v4.1-flash', 120, 8, self.now), 'all-in-one')
        self.assertEqual(attribution.lookup(self.path, 'deepseek-v4.1-flash-1017', 33, 2, self.now), 'minekuai')

    def test_ambiguous_rows_are_not_guessed(self):
        db = sqlite3.connect(self.path)
        db.execute('INSERT INTO request_stats(request_id,provider,model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?,?)',
                   ('r4', 'minekuai', 'deepseek-v4.1-flash', 120, 8, attribution._stamp(self.now)))
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
                   ' prompt_tokens INTEGER, completion_tokens INTEGER, timestamp TEXT)')
        db.execute('INSERT INTO request_stats(provider,model,prompt_tokens,completion_tokens,timestamp) VALUES(?,?,?,?,?)',
                   ('amd', 'glm-5.3-flash', 77, 5, attribution._stamp(self.now)))
        db.commit()
        # Keep the connection open so the WAL sidecar is still live, like the real service.
        self.assertEqual(attribution.lookup(wal, 'glm-5.3-flash', 77, 5, self.now), 'amd')
        db.close()

    def test_missing_database_is_safe(self):
        self.assertFalse(attribution.available('/nonexistent/stats.db'))
        self.assertIsNone(attribution.lookup('/nonexistent/stats.db', 'm', 1, 1, self.now))


if __name__ == '__main__':
    unittest.main()