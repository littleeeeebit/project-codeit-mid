"""One gold-2 validation report from whichever `rfp_assistant` is first on PYTHONPATH (the checked head, or the
merge base's worktree for comparison), on the data directory and database the flow passes in.

    python -B tools/verification/gold_contract.py <data-dir> <dsn> <out.json> [--init]

`--init` applies the importing code's idempotent schema statements to <dsn> first; the flow passes only private
copies. The report is written to <out.json>; validate_gold_v2 also writes `dev.validation.json` under <data-dir>.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    data_dir, dsn, out = Path(argv[0]), argv[1], Path(argv[2])
    os.environ.update(RFP_DATA_DIR=str(data_dir), RFP_GOLD_CONTRACT_DSN=dsn)
    from rfp_assistant.evaluation import evaluation
    from rfp_assistant.settings import load_settings
    from rfp_assistant.storage import store

    settings = load_settings(provider="fake", database_dsn_env="RFP_GOLD_CONTRACT_DSN")
    with store.database_lifecycle(settings.db_path):
        if "--init" in argv[3:]:
            store.init_schema(settings.db_path)
        report = evaluation.validate_gold_v2(settings, "dev")
        report["frozen_dataset_current"] = (evaluation.frozen_dataset(settings, "dev") or {}).get("current")
    out.write_text(json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")
    print(report["ok"], report["label"], len(report["errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
