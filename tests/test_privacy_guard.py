"""Tests for scripts/privacy_guard.sh, the pre-commit check that blocks secrets.

Each test makes a throwaway git repo in a temp folder, copies the guard into it,
stages one file and runs the guard. Exit code 1 = commit blocked, 0 = allowed.

Fake secrets are glued together from pieces at runtime, so this test file itself
does not contain anything the guard would block.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "privacy_guard.sh"

FAKE_DB_URL = "postgres" + "://someone:" + "secretpass" + "@db.example.com/app"
FAKE_TOKEN_LINE = "MCP_TOKEN" + "=" + "a" * 32


def run_guard(tmp_path: Path, filename: str, content: str) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(GUARD, repo / "scripts" / "privacy_guard.sh")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "add", "-f", filename], cwd=repo, check=True)

    return subprocess.run(
        ["bash", "scripts/privacy_guard.sh"], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "filename",
    [".env", "data.csv", "notes/00_OVERVIEW.md", "private-data/x.txt", "PLAN.md", "db.dump"],
)
def test_blocks_forbidden_files(tmp_path, filename):
    result = run_guard(tmp_path, filename, "hello\n")
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("content", [FAKE_DB_URL, FAKE_TOKEN_LINE])
def test_blocks_secrets_inside_files(tmp_path, content):
    result = run_guard(tmp_path, "app/config.py", content + "\n")
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize(
    "filename, content",
    [
        ("app/server.py", "print('hello')\n"),
        ("examples/moneyflow.example.csv", "Date,Amount\n2026-03-02 09:15:00,-3.40\n"),
        (".env.example", "MCP_TOKEN=\n"),
    ],
)
def test_allows_normal_files(tmp_path, filename, content):
    result = run_guard(tmp_path, filename, content)
    assert result.returncode == 0, result.stdout
