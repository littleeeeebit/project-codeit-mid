"""The Streamlit screens are a presentation-only shell: from the package they import `service` and nothing else."""

import ast
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "rfp_assistant" / "ui.py"
ALLOWED_STDLIB = {"__future__", "json", "re", "datetime"}


class UiBoundaryTest(unittest.TestCase):
    def test_ui_imports_only_the_service_from_the_package(self):
        package, outside = set(), set()
        for node in ast.walk(ast.parse(UI.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative: from . import x / from .x import y
                    package |= {node.module} if node.module else {a.name for a in node.names}
                else:
                    outside.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                outside |= {a.name.split(".")[0] for a in node.names}
        self.assertEqual(package, {"service"})
        self.assertLessEqual(outside, ALLOWED_STDLIB | {"streamlit"}, outside)

    def test_ui_reaches_no_internal_module_through_the_service(self):
        """`service.retrieval.x` would bypass the boundary as surely as an import would."""
        internals = {"retrieval", "generation", "store", "budget", "answers", "gold", "drafting", "evaluation",
                     "auth", "ingestion", "dense", "settings"}
        used = {n.attr for n in ast.walk(ast.parse(UI.read_text(encoding="utf-8")))
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "service"}
        self.assertFalse(used & internals, used & internals)


if __name__ == "__main__":
    unittest.main()
