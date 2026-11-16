import unittest
from pathlib import Path
class PackagingTests(unittest.TestCase):
    def test_runtime_assets_are_packaged(self):
        docker=Path('Dockerfile').read_text()
        for asset in ['management.py','model_discovery.py','console.css']:
            self.assertIn(asset,docker)
        self.assertIn('UNI_API_CONFIG_PATH: /config/api.yaml',Path('deploy/docker-compose.yml').read_text())
