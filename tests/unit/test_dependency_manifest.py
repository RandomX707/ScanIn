"""Every third-party module imported by production code (packages/) must be declared in requirements.txt.
A virtualenv built from that file alone must be able to run the application: numpy was once imported by the PDF edit worker without
being declared, which only a clean install can reveal."""
import ast
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STDLIB = set(sys.stdlib_module_names)


def _declared() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*==", line.strip())
        if match:
            names.add(match.group(1).lower().replace("_", "-").replace(".", "-"))
    return names


def _imported() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted((ROOT / "packages").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                if module not in STDLIB and module != "packages":
                    found.setdefault(module, str(path.relative_to(ROOT)))
    return found


def test_every_production_import_is_declared_in_requirements():
    declared, distributions, missing = _declared(), metadata.packages_distributions(), []
    for module, where in _imported().items():
        candidates = {name.lower().replace("_", "-").replace(".", "-") for name in distributions.get(module, [module])}
        if not candidates & declared:
            missing.append(f"{module} (first imported in {where})")
    assert not missing, "undeclared production dependencies: " + ", ".join(missing)


def test_numpy_is_pinned_for_the_pdf_edit_worker():
    assert "numpy" in _declared()
    assert "import numpy" in (ROOT / "packages/workers/pdf_edit_entry.py").read_text()
