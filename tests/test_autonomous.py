"""Local PRD 1.4 regressions; no Docker daemon or external network."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from aiohttp.test_utils import make_mocked_request
from audit_console import Settings, create_app

class EntrypointTests(unittest.TestCase):
    def test_wait_atomic_publish_exec_restart_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=root/'api.yaml'; marker=root/'started'
            native=root/'native'
            native.write_text('#!/bin/sh\nprintf "%s" "$$" > "$MARKER"\nexec sleep 30\n')
            native.chmod(0o700)
            env={**os.environ,'UNI_API_CONFIG_PATH':str(config),'MARKER':str(marker)}
            def launch():
                p=subprocess.Popen(['sh','deploy/wait-for-config.sh',str(native)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.addCleanup(lambda: p.poll() is None and (p.terminate(),p.wait(timeout=3)))
                return p
            def started(p):
                deadline=time.monotonic()+4
                while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(marker.exists())
                self.assertEqual(int(marker.read_text()),p.pid)
            p=launch(); time.sleep(.15)
            self.assertIsNone(p.poll()); self.assertFalse(marker.exists())
            temporary=root/'.pending'; temporary.write_text('{"synthetic":true}')
            time.sleep(.15); self.assertFalse(marker.exists())
            temporary.rename(config); started(p)
            p.send_signal(signal.SIGTERM); p.wait(timeout=3)
            marker.unlink(); p=launch(); started(p)
            p.terminate(); p.wait(timeout=3)
            config.unlink(); p=launch(); time.sleep(.1); p.terminate(); p.wait(timeout=3)

    def test_compose_directory_mount_no_profile_or_command(self):
        compose=Path('deploy/docker-compose.yml').read_text()
        self.assertNotIn('provider-configured',compose)
        self.assertIn('./uni-api/config:/config:rw',compose)
        self.assertNotIn('command:',compose)
        self.assertNotIn('docker.sock',compose)
        self.assertIn('restart: unless-stopped',compose)

class LANOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_lan_origin_checks_and_auth(self):
        app=create_app(Settings('http://localhost:8000','synthetic-admin'))
        from aiohttp import web
        async def ok(r): return web.Response(status=204)
        for origin,fetch,token,status in [
            ('http://192.168.10.2:8787','same-origin','synthetic-admin',204),
            ('http://evil.example','cross-site','synthetic-admin',403),
            ('null','same-origin','synthetic-admin',403),
            ('http://192.168.10.2:8787','cross-site','synthetic-admin',403),
            ('http://192.168.10.2:8787','same-origin','wrong',401)]:
            r=make_mocked_request('POST','/_audit/api/manage',headers={'Host':'192.168.10.2:8787','Origin':origin,'Sec-Fetch-Site':fetch,'Authorization':'Bearer '+token},app=app)
            self.assertEqual((await app.middlewares[0](r,ok)).status,status)
