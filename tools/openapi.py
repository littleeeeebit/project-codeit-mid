"""Write the API's OpenAPI schema to web/openapi.json, which web/ turns into TypeScript types before every build.

tests/test_api.py fails while the committed file differs from the API, so a changed route reaches the screens as
a type error rather than at run time. Run after changing src/rfp_assistant/api.py:

    python tools/openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfp_assistant.api import app  # noqa: E402

TARGET = ROOT / "web" / "openapi.json"


def schema_text() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    TARGET.write_text(schema_text(), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
