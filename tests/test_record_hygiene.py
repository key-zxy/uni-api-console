"""Record hygiene: probe requests are not logged and inline media is stripped."""

import re
import unittest

import audit_console
from audit_console import should_log, strip_inline_base64


class SkipProbeTests(unittest.TestCase):
    def test_capability_probes_are_not_logged(self):
        for path in ('/v1/models', '/api/v1/models', '/models', '/api/tags', '/version',
                     '/props', '/v1/props', '/api/show', '/favicon.ico', '/favicon.svg', '/', ''):
            self.assertFalse(should_log(path), path)

    def test_conversation_paths_are_logged(self):
        for path in ('/v1/chat/completions', '/v1/responses', '/v1/messages', '/v1/embeddings',
                     '/v1/images/generations', '/v1/audio/speech'):
            self.assertTrue(should_log(path), path)


class UpstreamHintTests(unittest.TestCase):
    def test_vendor_fingerprints_are_kept(self):
        hints = audit_console.upstream_hint({
            'x-magpie-provider': 'sensenova', 'x-magpie-model': 'sensenova/deepseek-flash',
            'x-new-api-version': 'v0.0.0-rc44', 'content-type': 'application/json'})
        self.assertEqual(hints['x-magpie-provider'], 'sensenova')
        self.assertEqual(hints['x-magpie-model'], 'sensenova/deepseek-flash')
        self.assertNotIn('content-type', hints)

    def test_no_fingerprint_returns_none(self):
        self.assertIsNone(audit_console.upstream_hint({'server': 'nginx'}))


class Base64StripTests(unittest.TestCase):
    def test_data_uri_is_replaced_with_size_marker(self):
        payload = b'{"model":"m","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,' + b'A' * 5000 + b'"}}]}]}'
        cleaned = strip_inline_base64(payload)
        self.assertNotIn(b'A' * 5000, cleaned)
        self.assertIn(b'[base64', cleaned)
        self.assertIn(b'data:image/png;base64', cleaned)
        # The rest of the JSON survives.
        self.assertIn(b'"model":"m"', cleaned)

    def test_bare_base64_run_is_stripped(self):
        body = b'{"image":"' + b'AbC+/' * 400 + b'"}'
        cleaned = strip_inline_base64(body)
        self.assertLess(len(cleaned), 200)
        self.assertIn(b'[base64', cleaned)

    def test_short_strings_are_left_alone(self):
        body = b'{"text":"short/base64-ish"}'
        self.assertEqual(strip_inline_base64(body), body)
        self.assertEqual(strip_inline_base64(b''), b'')


if __name__ == '__main__':
    unittest.main()