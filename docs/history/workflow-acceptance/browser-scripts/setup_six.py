import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # throwaway fixture environment for browser checks
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "src"))
from tests import fixtures
root = Path(sys.argv[1])
env = fixtures.make_env(root)
(root / "config.json").write_text(json.dumps({"provider": "fake", "fake_delay_seconds": 4.0}), encoding="utf-8")
