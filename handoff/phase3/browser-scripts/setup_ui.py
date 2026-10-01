import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # throwaway fixture environment for browser checks; fake tokens only
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "src"))
from tests import fixtures
from rfp_assistant import auth
root = Path(sys.argv[1])
env = fixtures.make_env(root)
cfg = root / "config.json"
cfg.write_text(json.dumps({"provider": "fake", "fake_delay_seconds": 4.0}), encoding="utf-8")
s = env.settings
tokens = {m: auth.provision(s, m, caps) for m, caps in
          [("kim", ["consultant"]), ("lee", ["consultant", "verifier"]), ("owner", ["consultant", "verifier", "budget_admin"])]}
(root / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
print(json.dumps({"data": str(s.data_dir), "source": str(s.source_dir), "config": str(cfg)}))
