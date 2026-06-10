"""
Tests for main module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from zenodo_release_drift.main import (
    CheckUserResult,
    DriftEngine,
    GitHubCollector,
    GitHubUserCollector,
    VersionMatcher,
    ZenodoCollector,
    check_user,
    explain_finding,
    lint_repo,
    lint_repo_explain,
)


def _mock_github(releases: list[str]) -> GitHubCollector:
    m = MagicMock(spec=GitHubCollector)
    m.get_releases.return_value = releases
    return m


def _mock_zenodo(versions: list[str]) -> ZenodoCollector:
    m = MagicMock(spec=ZenodoCollector)
    m.get_versions.return_value = versions
    return m


def _mock_user_collector(repos: list[str]) -> GitHubUserCollector:
    m = MagicMock(spec=GitHubUserCollector)
    m.username = "testuser"
    m.get_repos.return_value = repos
    return m


class TestGitHubCollector:
    def test_returns_tag_names(self) -> None:
        release = MagicMock()
        release.tag_name = "v1.0.0"
        gh = MagicMock()
        gh.get_repo.return_value.get_releases.return_value = [release]
        collector = GitHubCollector("owner", "repo", client=gh)
        assert collector.get_releases() == ["v1.0.0"]

    def test_returns_empty_on_exception(self) -> None:
        gh = MagicMock()
        gh.get_repo.side_effect = GithubException(404, "Not Found", None)
        collector = GitHubCollector("owner", "repo", client=gh)
        assert collector.get_releases() == []


class TestGitHubUserCollector:
    def test_returns_repo_names(self) -> None:
        repo = MagicMock()
        repo.name = "my-repo"
        gh = MagicMock()
        gh.get_user.return_value.get_repos.return_value = [repo]
        collector = GitHubUserCollector("testuser", client=gh)
        assert collector.get_repos() == ["my-repo"]

    def test_raises_runtime_on_401(self) -> None:
        gh = MagicMock()
        exc = GithubException(401, "Unauthorized", None)
        gh.get_user.return_value.get_repos.side_effect = exc
        collector = GitHubUserCollector("testuser", client=gh)
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
            collector.get_repos()

    def test_raises_runtime_on_403(self) -> None:
        gh = MagicMock()
        exc = GithubException(403, "Forbidden", None)
        gh.get_user.return_value.get_repos.side_effect = exc
        collector = GitHubUserCollector("testuser", client=gh)
        with pytest.raises(RuntimeError, match="rate limits"):
            collector.get_repos()


class TestVersionMatcher:
    def test_no_drift(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.0.0", "v1.1.0"], ["v1.0.0", "v1.1.0"])
        assert result["missing_versions"] == []
        assert not result["is_behind"]

    def test_missing_version(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.0.0", "v1.1.0"], ["v1.0.0"])
        assert "1.1.0" in result["missing_versions"]

    def test_behind(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.3.0"], ["v1.2.0"])
        assert result["is_behind"]
        assert result["latest_github"] == "1.3.0"
        assert result["latest_zenodo"] == "1.2.0"

    def test_strips_v_prefix(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.0.0"], ["1.0.0"])
        assert result["missing_versions"] == []

    def test_proper_semver_ordering(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.10.0"], ["v1.9.0"])
        assert result["is_behind"]

    def test_empty_zenodo(self) -> None:
        matcher = VersionMatcher()
        result = matcher.match(["v1.0.0"], [])
        assert "1.0.0" in result["missing_versions"]
        assert not result["is_behind"]


class TestDriftEngine:
    def test_zrd001_detected(self) -> None:
        engine = DriftEngine(
            _mock_github(["v1.0.0", "v1.1.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        findings = engine.detect("owner", "repo")
        codes = [f["code"] for f in findings]
        assert "ZRD001" in codes

    def test_zrd002_detected(self) -> None:
        engine = DriftEngine(
            _mock_github(["v1.3.0"]),
            _mock_zenodo(["v1.2.0"]),
        )
        findings = engine.detect("owner", "repo")
        codes = [f["code"] for f in findings]
        assert "ZRD002" in codes

    def test_no_findings_when_synced(self) -> None:
        engine = DriftEngine(
            _mock_github(["v1.0.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        assert engine.detect("owner", "repo") == []


class TestExplainFinding:
    def test_zrd001_explanation(self) -> None:
        finding = {
            "code": "ZRD001",
            "severity": "high",
            "message": "...",
            "version": "1.3.0",
        }
        text = explain_finding(finding)
        assert "1.3.0" in text
        assert "Zenodo" in text

    def test_zrd002_explanation(self) -> None:
        finding = {
            "code": "ZRD002",
            "severity": "high",
            "message": "...",
            "latest_github": "1.3.0",
            "latest_zenodo": "1.2.0",
        }
        text = explain_finding(finding)
        assert "1.3.0" in text
        assert "1.2.0" in text

    def test_unknown_finding(self) -> None:
        assert explain_finding({"code": "ZRD999"}) == "Unknown finding type."


class TestLintRepo:
    def test_returns_list(self) -> None:
        findings = lint_repo(
            "owner",
            "repo",
            _mock_github(["v1.0.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        assert isinstance(findings, list)

    def test_drift_detected(self) -> None:
        findings = lint_repo(
            "owner",
            "repo",
            _mock_github(["v1.0.0", "v1.1.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        assert any(f["code"] == "ZRD001" for f in findings)


class TestLintRepoExplain:
    def test_no_drift_message(self) -> None:
        report = lint_repo_explain(
            "owner",
            "repo",
            _mock_github(["v1.0.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        assert "No drift detected" in report

    def test_report_contains_finding_code(self) -> None:
        report = lint_repo_explain(
            "owner",
            "repo",
            _mock_github(["v1.0.0", "v1.1.0"]),
            _mock_zenodo(["v1.0.0"]),
        )
        assert "ZRD001" in report


class TestCheckUser:
    def test_returns_check_user_result(self) -> None:
        collector = _mock_user_collector([])
        result = check_user("nobody", collector)
        assert isinstance(result, CheckUserResult)

    def test_skips_repos_with_no_zenodo_records(self) -> None:
        collector = _mock_user_collector(["repo-a"])
        with patch("zenodo_release_drift.main.ZenodoCollector") as MockZenodo:
            MockZenodo.return_value.get_versions.return_value = []
            result = check_user("testuser", collector)
        assert result.findings == {}
        assert result.repos_with_zenodo == 0
        assert result.repos_total == 1

    def test_skips_repos_with_no_drift(self) -> None:
        collector = _mock_user_collector(["repo-a"])
        with (
            patch("zenodo_release_drift.main.ZenodoCollector") as MockZenodo,
            patch("zenodo_release_drift.main.lint_repo", return_value=[]),
        ):
            MockZenodo.return_value.get_versions.return_value = ["1.0.0"]
            result = check_user("testuser", collector)
        assert result.findings == {}
        assert result.repos_with_zenodo == 1

    def test_includes_repos_with_zenodo_and_drift(self) -> None:
        finding = {
            "code": "ZRD001",
            "severity": "high",
            "message": "...",
            "version": "1.1.0",
        }
        collector = _mock_user_collector(["repo-a"])
        with (
            patch("zenodo_release_drift.main.ZenodoCollector") as MockZenodo,
            patch("zenodo_release_drift.main.lint_repo", return_value=[finding]),
        ):
            MockZenodo.return_value.get_versions.return_value = ["1.0.0"]
            result = check_user("testuser", collector)
        assert "testuser/repo-a" in result.findings

    def test_empty_when_no_repos(self) -> None:
        collector = _mock_user_collector([])
        result = check_user("nobody", collector)
        assert result.findings == {}
        assert result.repos_total == 0
