import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # throwaway fixture environment for browser checks; fake tokens only
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "src"))
from tests import fixtures
from rfp_assistant import auth
root = Path(sys.argv[1])
env = fixtures.make_env(root)
(root / "config.json").write_text(json.dumps({"provider": "fake", "fake_delay_seconds": 4.0}), encoding="utf-8")
tokens = {f"m{i}": auth.provision(env.settings, f"m{i}", ["consultant"]) for i in range(1, 7)}
tokens["owner"] = auth.provision(env.settings, "owner", ["consultant", "verifier", "budget_admin"])
(root / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
