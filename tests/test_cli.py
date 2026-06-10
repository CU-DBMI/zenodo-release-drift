"""
Tests for the CLI module.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from zenodo_release_drift.cli import _findings_to_rows, app
from zenodo_release_drift.main import CheckUserResult

runner = CliRunner()


class TestFindingsToRows:
    def test_zrd001_collapsed(self) -> None:
        findings = [
            {
                "code": "ZRD001",
                "severity": "high",
                "message": "...",
                "version": "1.0.0",
            },
            {
                "code": "ZRD001",
                "severity": "high",
                "message": "...",
                "version": "1.1.0",
            },
        ]
        rows = _findings_to_rows(findings)
        assert len(rows) == 1
        assert rows[0][0] == "ZRD001"
        assert "2 release(s)" in rows[0][2]
        assert "1.0.0" in rows[0][2]
        assert "1.1.0" in rows[0][2]

    def test_zrd002_row(self) -> None:
        findings = [
            {
                "code": "ZRD002",
                "severity": "high",
                "message": "...",
                "latest_github": "1.2.0",
                "latest_zenodo": "1.0.0",
            }
        ]
        rows = _findings_to_rows(findings)
        assert rows[0][0] == "ZRD002"
        assert "1.2.0" in rows[0][2]
        assert "1.0.0" in rows[0][2]

    def test_both_codes(self) -> None:
        findings = [
            {
                "code": "ZRD001",
                "severity": "high",
                "message": "...",
                "version": "1.1.0",
            },
            {
                "code": "ZRD002",
                "severity": "high",
                "message": "...",
                "latest_github": "1.1.0",
                "latest_zenodo": "1.0.0",
            },
        ]
        rows = _findings_to_rows(findings)
        assert len(rows) == len(findings)


class TestLintCommand:
    def test_invalid_repo_format(self) -> None:
        result = runner.invoke(app, ["lint", "noslash"])
        assert result.exit_code == 1
        assert "owner/repo" in result.output

    def test_no_drift_output(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock_lint:
            mock_lint.return_value = []
            result = runner.invoke(app, ["lint", "owner/repo"])
        assert result.exit_code == 0
        assert "owner/repo" in result.output
        assert "No drift detected" in result.output

    def test_zrd001_collapsed_in_output(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock_lint:
            mock_lint.return_value = [
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": "...",
                    "version": "1.0.0",
                },
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": "...",
                    "version": "1.1.0",
                },
            ]
            result = runner.invoke(app, ["lint", "owner/repo"])
        assert result.exit_code == 1
        assert "ZRD001" in result.output
        assert result.output.count("ZRD001") == 1

    def test_json_output(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock_lint:
            mock_lint.return_value = [
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": "...",
                    "version": "1.1.0",
                }
            ]
            result = runner.invoke(app, ["lint", "owner/repo", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data[0]["code"] == "ZRD001"

    def test_markdown_output(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock_lint:
            mock_lint.return_value = []
            result = runner.invoke(app, ["lint", "owner/repo", "--markdown"])
        assert result.exit_code == 0
        assert "# Repository" in result.output

    def test_explain_output(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo_explain") as mock_explain:
            mock_explain.return_value = "# Repository: owner/repo\n\nNo drift."
            result = runner.invoke(app, ["lint", "owner/repo", "--explain"])
        assert result.exit_code == 0
        assert "Repository" in result.output


class TestCheckCommand:
    def _make_result(
        self,
        findings: dict,  # type: ignore[type-arg]
        repos_total: int = 10,
        repos_with_zenodo: int = 2,
    ) -> CheckUserResult:
        return CheckUserResult(findings, repos_total, repos_with_zenodo)

    def test_no_drift_message(self) -> None:
        with patch("zenodo_release_drift.cli.check_user") as mock:
            mock.return_value = self._make_result({})
            result = runner.invoke(app, ["check", "d33bs"])
        assert result.exit_code == 0
        assert "No drift detected" in result.output
        assert "10 repos found" in result.output

    def test_repos_with_drift_listed(self) -> None:
        findings = {
            "d33bs/repo-a": [
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": "msg",
                    "version": "1.0.0",
                }
            ]
        }
        with patch("zenodo_release_drift.cli.check_user") as mock:
            mock.return_value = self._make_result(findings)
            result = runner.invoke(app, ["check", "d33bs"])
        assert result.exit_code == 0
        assert "d33bs/repo-a" in result.output
        assert "ZRD001" in result.output

    def test_json_output(self) -> None:
        with patch("zenodo_release_drift.cli.check_user") as mock:
            mock.return_value = self._make_result({"d33bs/repo-a": []})
            result = runner.invoke(app, ["check", "d33bs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "d33bs/repo-a" in data

    def test_single_repo_no_drift(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock:
            mock.return_value = []
            result = runner.invoke(app, ["check", "owner/repo"])
        assert result.exit_code == 0
        assert "No drift detected" in result.output

    def test_single_repo_json(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo") as mock:
            mock.return_value = [
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": ".",
                    "version": "1.0.0",
                }
            ]
            result = runner.invoke(app, ["check", "owner/repo", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data[0]["code"] == "ZRD001"

    def test_single_repo_explain(self) -> None:
        with patch("zenodo_release_drift.cli.lint_repo_explain") as mock:
            mock.return_value = "# Repository: owner/repo\n\nNo drift."
            result = runner.invoke(app, ["check", "owner/repo", "--explain"])
        assert result.exit_code == 0
        assert "Repository" in result.output


class TestVersionCommand:
    def test_version_output(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "zenodo-release-drift" in result.output
