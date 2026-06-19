"""
Tests for main module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from github import GithubException

from zenodo_release_drift.main import (
    CheckUserResult,
    DriftEngine,
    GitHubCollector,
    GitHubUserCollector,
    VersionMatcher,
    ZenodoCollector,
    ZenodoUploader,
    check_user,
    explain_finding,
    fix_repo,
    lint_repo,
    lint_repo_explain,
)


def _ok(json_data: object = None) -> MagicMock:
    """Return a mock httpx response with status 200."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status.return_value = None
    return resp


def _err(status_code: int, text: str = "error") -> MagicMock:
    """Return a mock httpx response that raises HTTPStatusError on raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    resp.raise_for_status.side_effect = exc
    return resp


def _mock_dl_client() -> MagicMock:
    """Return a mock httpx.Client that simulates downloading an archive."""
    dl_resp = MagicMock()
    dl_resp.raise_for_status.return_value = None
    dl_resp.content = b"zipdata"
    dl_client = MagicMock()
    dl_client.__enter__ = MagicMock(return_value=dl_client)
    dl_client.__exit__ = MagicMock(return_value=False)
    dl_client.get.return_value = dl_resp
    return dl_client


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


class TestZenodoCollector:
    def test_owner_and_repo_lowercased(self) -> None:
        collector = ZenodoCollector("CytoMining", "CytoTable")
        assert collector.owner == "cytomining"
        assert collector.repo == "cytotable"

    def test_repo_url_uses_lowercase(self) -> None:
        collector = ZenodoCollector("CytoMining", "CytoTable")
        assert collector._repo_url() == "https://github.com/cytomining/cytotable"

    def test_hit_belongs_via_related_identifier_mixed_case(self) -> None:
        # Zenodo stores the repo's original casing; matching must be
        # case-insensitive since the collector lowercases owner/repo.
        collector = ZenodoCollector("cytomining", "cytotable")
        hit = {
            "metadata": {
                "related_identifiers": [
                    {
                        "relation": "isSupplementTo",
                        "identifier": "https://github.com/cytomining/CytoTable/tree/v1.2.1",
                    }
                ]
            }
        }
        assert collector._hit_belongs_to_repo(hit) is True

    def test_hit_belongs_via_code_repository_mixed_case(self) -> None:
        collector = ZenodoCollector("cytomining", "cytotable")
        hit = {
            "metadata": {
                "custom": {
                    "code:codeRepository": "https://github.com/cytomining/CytoTable"
                }
            }
        }
        assert collector._hit_belongs_to_repo(hit) is True

    def test_hit_does_not_belong_to_other_repo(self) -> None:
        collector = ZenodoCollector("cytomining", "cytotable")
        hit = {
            "metadata": {
                "related_identifiers": [
                    {"identifier": "https://github.com/other/project/tree/v1.0.0"}
                ]
            }
        }
        assert collector._hit_belongs_to_repo(hit) is False


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


class TestZenodoUploader:
    def _uploader(self, client: MagicMock) -> ZenodoUploader:
        return ZenodoUploader(token="tok", client=client)

    def _no_candidates(self) -> list[object]:
        """Two empty get responses: public-oldest then depositions."""
        return [_ok({"hits": {"hits": []}}), _ok([])]

    def test_find_candidates_public_oldest_first(self) -> None:
        record_id = 42
        client = MagicMock()
        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": record_id}]}}),  # public oldest
            _ok([]),  # depositions
        ]
        candidates = self._uploader(client)._find_candidate_record_ids(client, "o", "r")
        assert candidates[0] == (record_id, "public-oldest")

    def test_find_candidates_deduplicates(self) -> None:
        record_id = 42
        client = MagicMock()
        # Same ID appears in both public and depositions
        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": record_id}]}}),
            _ok([{"id": record_id}]),
        ]
        candidates = self._uploader(client)._find_candidate_record_ids(client, "o", "r")
        assert len(candidates) == 1

    def test_find_candidates_empty(self) -> None:
        client = MagicMock()
        client.get.side_effect = self._no_candidates()
        candidates = self._uploader(client)._find_candidate_record_ids(client, "o", "r")
        assert candidates == []

    def test_find_candidates_non_200(self) -> None:
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        client.get.return_value = resp
        candidates = self._uploader(client)._find_candidate_record_ids(client, "o", "r")
        assert candidates == []

    def test_find_records_by_concept_doi(self) -> None:
        client = MagicMock()
        candidates = self._uploader(client)._find_records_by_concept_doi(
            client, "10.5281/zenodo.55"
        )
        # ID is extracted directly from the DOI — no HTTP call needed.
        assert candidates == [(55, "concept-doi")]
        client.get.assert_not_called()

    def test_find_records_by_concept_doi_invalid(self) -> None:
        client = MagicMock()
        candidates = self._uploader(client)._find_records_by_concept_doi(
            client, "not-a-doi"
        )
        assert candidates == []

    def test_upload_release_concept_doi_override(self) -> None:
        """--concept-doi bypasses the repo search and uses the named concept."""
        client = MagicMock()
        draft = {
            "id": 200,
            "links": {"bucket": "http://bucket/200"},
            "files": [],
        }
        published = {
            "doi": "10.5281/zenodo.200",
            "conceptdoi": "10.5281/zenodo.100",
            "links": {"html": ""},
        }
        # concept-doi derives ID from the DOI string (no search GET).
        # Only GETs are the draft fetch after newversion.
        client.get.side_effect = [_ok(draft)]
        client.post.side_effect = [
            _ok({"links": {"latest_draft": "http://draft/200"}}),  # newversion
            _ok(published),  # publish
        ]
        client.put.return_value = _ok()

        with patch(
            "zenodo_release_drift.main.httpx.Client", return_value=_mock_dl_client()
        ):
            result = self._uploader(client).upload_release(
                "o", "repo", "v2.0.0", "2.0.0", concept_doi="10.5281/zenodo.100"
            )

        assert result["status"] == "published"
        assert result["concept_doi"] == "10.5281/zenodo.100"
        # Only one GET (draft fetch) — no repo search, no concept search.
        assert client.get.call_count == 1

    def test_upload_release_new_concept(self) -> None:
        client = MagicMock()
        deposition = {"id": 1, "links": {"bucket": "http://bucket"}, "files": []}
        published = {
            "doi": "10.5281/zenodo.1",
            "conceptdoi": "10.5281/zenodo.concept",
            "links": {"html": "http://zenodo.org/record/1"},
        }
        # depositions empty → public records empty → falls back to new_deposition
        client.get.side_effect = [_ok({"hits": {"hits": []}}), _ok([])]
        client.post.side_effect = [_ok(deposition), _ok(published)]
        client.put.return_value = _ok()

        with patch(
            "zenodo_release_drift.main.httpx.Client", return_value=_mock_dl_client()
        ):
            result = self._uploader(client).upload_release(
                "o", "repo", "v1.0.0", "1.0.0"
            )

        assert result["status"] == "published"
        assert result["doi"] == "10.5281/zenodo.1"
        assert result["concept_doi"] == "10.5281/zenodo.concept"

    def test_upload_release_new_version_from_existing(self) -> None:
        client = MagicMock()
        draft = {
            "id": 99,
            "links": {"bucket": "http://bucket/99"},
            "files": [{"id": "f1"}],
        }
        newversion_resp = _ok({"links": {"latest_draft": "http://draft/99"}})
        published = {
            "doi": "10.5281/zenodo.99",
            "conceptdoi": "10.5281/zenodo.0",
            "links": {"html": ""},
        }

        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": 7}]}}),  # public-oldest search
            _ok([]),  # depositions search
            _ok(draft),  # fetch draft after newversion
        ]
        client.post.side_effect = [newversion_resp, _ok(published)]
        client.delete.return_value = _ok()
        client.put.return_value = _ok()

        with patch(
            "zenodo_release_drift.main.httpx.Client", return_value=_mock_dl_client()
        ):
            result = self._uploader(client).upload_release(
                "o", "repo", "v1.1.0", "1.1.0"
            )

        assert result["status"] == "published"
        # delete was called once to clear the inherited file
        client.delete.assert_called_once()

    def test_upload_release_403_includes_hint(self) -> None:
        client = MagicMock()
        # One candidate via public-oldest; depositions empty.
        # newversion fails 403 → new_deposition also fails 403 → outer handler.
        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": 7}]}}),
            _ok([]),
        ]
        client.post.return_value = _err(403)

        result = self._uploader(client).upload_release("o", "repo", "v1.0.0", "1.0.0")

        assert result["status"] == "error"
        assert "hint" in result
        assert "403" in result["hint"]

    def test_upload_release_404_includes_hint(self) -> None:
        client = MagicMock()
        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": 7}]}}),
            _ok([]),
        ]
        client.post.return_value = _err(404)

        result = self._uploader(client).upload_release("o", "repo", "v1.0.0", "1.0.0")

        assert result["status"] == "error"
        assert "hint" in result
        assert "404" in result["hint"]

    def test_upload_release_newversion_failure_falls_back_to_new_deposition(
        self,
    ) -> None:
        client = MagicMock()
        deposition = {"id": 2, "links": {"bucket": "http://bucket/2"}, "files": []}
        published = {
            "doi": "10.5281/zenodo.2",
            "conceptdoi": None,
            "links": {"html": ""},
        }
        client.get.side_effect = [
            _ok({"hits": {"hits": [{"id": 7}]}}),
            _ok([]),
        ]
        # First post (newversion) fails, second post (new deposition) succeeds,
        # third post (publish) succeeds.
        client.post.side_effect = [_err(404), _ok(deposition), _ok(published)]
        client.put.return_value = _ok()

        with patch(
            "zenodo_release_drift.main.httpx.Client", return_value=_mock_dl_client()
        ):
            result = self._uploader(client).upload_release(
                "o", "repo", "v1.0.0", "1.0.0"
            )

        assert result["status"] == "published"
        assert "warning" in result

    def test_upload_release_other_http_error_no_hint(self) -> None:
        client = MagicMock()
        client.get.side_effect = [_ok({"hits": {"hits": []}}), _ok([])]
        client.post.return_value = _err(500, "server error")

        result = self._uploader(client).upload_release("o", "repo", "v1.0.0", "1.0.0")

        assert result["status"] == "error"
        assert "hint" not in result
        assert "500" in result["error"]

    def test_upload_release_request_error(self) -> None:
        client = MagicMock()
        client.get.side_effect = httpx.RequestError("timeout")

        result = self._uploader(client).upload_release("o", "repo", "v1.0.0", "1.0.0")

        assert result["status"] == "error"
        assert "timeout" in result["error"]

    def test_sandbox_uses_sandbox_url(self) -> None:
        uploader = ZenodoUploader(token="tok", sandbox=True, client=MagicMock())
        assert "sandbox" in uploader.base_url


class TestFixRepo:
    def _mock_gh(
        self,
        releases: list[str],
        dates: dict[str, str] | None = None,
    ) -> GitHubCollector:
        m = MagicMock(spec=GitHubCollector)
        m.get_releases.return_value = releases
        m.get_release_dates.return_value = dates or {}
        return m

    def _mock_zen(self, versions: list[str]) -> ZenodoCollector:
        m = MagicMock(spec=ZenodoCollector)
        m.get_versions.return_value = versions
        return m

    def _published(self, version: str, tag: str) -> dict:  # type: ignore[type-arg]
        return {
            "version": version,
            "tag": tag,
            "doi": f"10.5281/zenodo.{version}",
            "concept_doi": "10.5281/zenodo.0",
            "zenodo_url": "",
            "status": "published",
        }

    def test_no_missing_returns_empty(self) -> None:
        gh = self._mock_gh(["v1.0.0"])
        zen = self._mock_zen(["1.0.0"])
        result = fix_repo(
            "o", "repo", token="tok", github_collector=gh, zenodo_collector=zen
        )
        assert result == []

    def test_single_version_upload(self) -> None:
        gh = self._mock_gh(["v1.0.0", "v1.1.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.return_value = self._published(
                "1.1.0", "v1.1.0"
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                version="1.1.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        assert result[0]["status"] == "published"
        MockUp.return_value.upload_release.assert_called_once_with(
            "o",
            "repo",
            tag="v1.1.0",
            version="1.1.0",
            concept_doi=None,
            publication_date=None,
            on_progress=None,
        )

    def test_single_version_with_v_prefix(self) -> None:
        gh = self._mock_gh(["v2.0.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.return_value = self._published(
                "2.0.0", "v2.0.0"
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                version="v2.0.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        assert result[0]["status"] == "published"

    def test_single_version_not_found_on_github(self) -> None:
        gh = self._mock_gh(["v1.0.0"])
        result = fix_repo(
            "o", "repo", token="tok", version="9.9.9", github_collector=gh
        )
        assert result[0]["status"] == "error"
        assert "No GitHub release" in result[0]["error"]

    def test_single_version_skipped_when_already_archived(self) -> None:
        # Version is already on Zenodo and --force not given → skip, no upload.
        gh = self._mock_gh(["v1.1.0"])
        zen = self._mock_zen(["v1.1.0"])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                version="1.1.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        assert result[0]["status"] == "skipped"
        assert "already archived" in result[0]["reason"]
        MockUp.return_value.upload_release.assert_not_called()

    def test_single_version_force_reuploads_existing(self) -> None:
        # --force uploads even when the version is already archived.
        gh = self._mock_gh(["v1.1.0"])
        zen = self._mock_zen(["v1.1.0"])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.return_value = self._published(
                "1.1.0", "v1.1.0"
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                version="1.1.0",
                force=True,
                github_collector=gh,
                zenodo_collector=zen,
            )
        assert result[0]["status"] == "published"
        MockUp.return_value.upload_release.assert_called_once()

    def test_all_missing_uploaded(self) -> None:
        gh = self._mock_gh(["v1.0.0", "v1.1.0"])
        zen = self._mock_zen(["1.0.0"])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.return_value = self._published(
                "1.1.0", "v1.1.0"
            )
            result = fix_repo(
                "o", "repo", token="tok", github_collector=gh, zenodo_collector=zen
            )
        assert len(result) == 1
        assert result[0]["version"] == "1.1.0"

    def test_from_version_filters_lower_versions(self) -> None:
        gh = self._mock_gh(["v1.0.0", "v1.1.0", "v1.2.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                from_version="1.1.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert "1.0.0" not in uploaded
        assert "1.1.0" in uploaded
        assert "1.2.0" in uploaded

    def test_to_version_filters_higher_versions(self) -> None:
        gh = self._mock_gh(["v1.0.0", "v1.1.0", "v1.2.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                to_version="1.1.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert "1.0.0" in uploaded
        assert "1.1.0" in uploaded
        assert "1.2.0" not in uploaded

    def test_from_and_to_version_combined(self) -> None:
        gh = self._mock_gh(["v0.9.0", "v1.0.0", "v1.1.0", "v1.2.0", "v2.0.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                from_version="1.0.0",
                to_version="1.1.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert uploaded == ["1.0.0", "1.1.0"]

    def test_range_excludes_unparseable_versions(self) -> None:
        gh = self._mock_gh(["v1.0.0", "nightly", "v1.1.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                from_version="1.0.0",
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert "nightly" not in uploaded

    def test_since_latest_uploads_only_newer_than_zenodo(self) -> None:
        # Zenodo's newest archive is 1.1.0; GitHub has older and newer tags.
        # --since-latest must upload only versions strictly above 1.1.0,
        # skipping the older missing 1.0.0 even though it is absent from Zenodo.
        gh = self._mock_gh(["v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0"])
        zen = self._mock_zen(["1.1.0"])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                since_latest=True,
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert uploaded == ["1.2.0", "1.3.0"]

    def test_since_latest_with_empty_zenodo_uploads_all_missing(self) -> None:
        # No archive on Zenodo yet → no floor → every missing version uploads.
        gh = self._mock_gh(["v1.0.0", "v1.1.0"])
        zen = self._mock_zen([])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: (
                self._published(str(kw["version"]), str(kw["tag"]))
            )
            result = fix_repo(
                "o",
                "repo",
                token="tok",
                since_latest=True,
                github_collector=gh,
                zenodo_collector=zen,
            )
        uploaded = [r["version"] for r in result]
        assert uploaded == ["1.0.0", "1.1.0"]

    def test_missing_versions_uploaded_in_ascending_semver_order(self) -> None:
        # GitHub returns newest-first; uploads must happen oldest-first so the
        # Zenodo newversion chain reflects semver order.
        gh = self._mock_gh(["v1.2.0", "v1.1.0", "v1.0.0"])
        zen = self._mock_zen([])
        upload_order: list[str] = []

        def _capture(**kwargs: object) -> dict[str, object]:
            upload_order.append(str(kwargs["version"]))
            ver = str(kwargs["version"])
            return self._published(ver, f"v{ver}")

        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.side_effect = lambda *_, **kw: _capture(
                **kw
            )
            fix_repo(
                "o", "repo", token="tok", github_collector=gh, zenodo_collector=zen
            )

        assert upload_order == ["1.0.0", "1.1.0", "1.2.0"]

    def test_sandbox_flag_forwarded(self) -> None:
        gh = self._mock_gh(["v1.0.0"])
        zen = self._mock_zen(["1.0.0"])
        with patch("zenodo_release_drift.main.ZenodoUploader") as MockUp:
            MockUp.return_value.upload_release.return_value = self._published(
                "1.0.0", "v1.0.0"
            )
            fix_repo(
                "o",
                "repo",
                token="tok",
                sandbox=True,
                github_collector=gh,
                zenodo_collector=zen,
            )
        MockUp.assert_called_once_with(token="tok", sandbox=True)


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
