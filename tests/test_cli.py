from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
from typer.testing import CliRunner

from openflowbi.cli import app

runner = CliRunner()


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def _write_issues_parquet(out_dir, issue_ids):
    table_dir = out_dir / "jira_raw" / "issues"
    table_dir.mkdir(parents=True)
    # created_at/updated_at as a real timestamp type, not a string: matches
    # what dlt's normalizer actually writes for these columns (see
    # quality/checks.py's comment), confirmed against a live extract.
    timestamp = pa.timestamp("us", tz="UTC")
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    table = pa.table(
        {
            "instance_id": ["inst-a"] * len(issue_ids),
            # bigint from M7.2 (docs/phase2-postgres-design.md §3).
            "issue_id": pa.array(issue_ids, type=pa.int64()),
            "issue_key": [f"PROJ-{i}" for i in issue_ids],
            "created_at": pa.array([ts] * len(issue_ids), type=timestamp),
            "updated_at": pa.array([ts] * len(issue_ids), type=timestamp),
        }
    )
    pq.write_table(table, table_dir / "load1.1.parquet")


def test_quality_check_passes_on_well_formed_issues(tmp_path):
    _write_issues_parquet(tmp_path, [1, 2])
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_quality_check_fails_on_duplicate_issue_id(tmp_path):
    # rule 4 (CLAUDE.md): issue_id is the only identity — never duplicated.
    _write_issues_parquet(tmp_path, [1, 1])
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_quality_check_reports_missing_parquet(tmp_path):
    result = runner.invoke(app, ["quality", "check", "issues", "--out-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "No Parquet files found" in result.stdout


def test_quality_check_rejects_unknown_table(tmp_path):
    result = runner.invoke(app, ["quality", "check", "bogus", "--out-dir", str(tmp_path)])
    assert result.exit_code != 0
