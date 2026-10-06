"""Infrastructure startup must never select or replace an application database."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class PostgreSQLLauncherTest(unittest.TestCase):
    def test_start_preserves_explicit_dsn_and_leaves_missing_dsn_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools/infra").mkdir(parents=True)
            (root / ".runtime").mkdir()
            (root / ".runtime/postgresql.env").write_text("BIDMATE_POSTGRES_PASSWORD=fixture\n", encoding="utf-8")
            script = root / "tools/infra/start-postgresql.ps1"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "tools/infra/start-postgresql.ps1", script)
            for configured in (True, False):
                command = "function docker { $global:LASTEXITCODE = 0 }; "
                command += "$env:RFP_DATABASE_DSN = 'explicit-import'; " if configured else \
                    "Remove-Item Env:RFP_DATABASE_DSN -ErrorAction SilentlyContinue; "
                command += f". '{script.as_posix()}'; "
                expected = "'explicit-import'" if configured else "$null"
                command += f"if ($env:RFP_DATABASE_DSN -ne {expected}) {{ throw 'Application DSN was changed' }}"
                result = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
