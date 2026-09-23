"""Enforces the jira/ boundary rule: src/openflowbi/jira/ imports no destination, no
database, no filesystem. It speaks HTTP and returns plain objects.

Scans every .py file under jira/ at test time, so this stays enforced as
fields.py / changelog.py / flatten.py are added in later milestones without
needing manual updates here.
"""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src" / "openflowbi"
JIRA_DIR = SRC / "jira"
# jira/ builds every client through cloud/http.py, so that module is held to the same rule.
# cloud/tls.py writes the mTLS key files and is only ever imported by the CLI.
JIRA_DEPENDENCIES = (SRC / "cloud" / "http.py",)

FORBIDDEN_MODULE_PREFIXES = (
    "dlt.pipeline",
    "dlt.destinations",
    "dlt.common.destination",
    "duckdb",
    "pyarrow",
    "sqlite3",
    "psycopg2",
    "psycopg",
    "tempfile",
    "shutil",
    "openflowbi.cloud.tls",
)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _open_call_lines(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ]


def test_jira_package_has_no_destination_db_or_filesystem_imports():
    violations = []
    for path in [*sorted(JIRA_DIR.glob("*.py")), *JIRA_DEPENDENCIES]:
        tree = ast.parse(path.read_text(), filename=str(path))
        for module in _imported_modules(tree):
            if any(module == p or module.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES):
                violations.append(f"{path.name}: imports {module}")
        violations.extend(f"{path.name}:{ln}: calls open()" for ln in _open_call_lines(tree))
    assert not violations, "\n".join(violations)
