"""
Core logic for zenodo-release-drift.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from github import Auth, Github, GithubException
from packaging.version import InvalidVersion, Version

_TIMEOUT = httpx.Timeout(10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0)

ZENODO_BASE_URL = "https://zenodo.org/api"
ZENODO_SANDBOX_BASE_URL = "https://sandbox.zenodo.org/api"


def _github_client() -> Github:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return Github(auth=Auth.Token(token))
    return Github()


def _normalize(version: str) -> str:
    """Strip a leading 'v' from a version string."""
    return version.lstrip("v")


def _parse(version: str) -> Version | None:
    try:
        return Version(_normalize(version))
    except InvalidVersion:
        return None


class GitHubCollector:
    """Collects GitHub releases."""

    def __init__(
        self,
        owner: str,
        repo: str,
        client: Github | None = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self._client = client

    def get_releases(self) -> list[str]:
        """Return release tag names from GitHub."""
        gh = self._client or _github_client()
        try:
            gh_repo = gh.get_repo(f"{self.owner}/{self.repo}")
            return [r.tag_name for r in gh_repo.get_releases()]
        except GithubException:
            return []


class ZenodoCollector:
    """Collects Zenodo records for a GitHub repository."""

    BASE_URL = "https://zenodo.org/api"

    def __init__(
        self, owner: str, repo: str, client: httpx.Client | None = None
    ) -> None:
        self.owner = owner
        self.repo = repo
        self._client = client
        self._cache: list[str] | None = None

    def _repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def _hit_belongs_to_repo(self, hit: dict[str, Any]) -> bool:
        """Return True if the Zenodo record is linked to this GitHub repo."""
        meta = hit.get("metadata", {})
        repo_url = self._repo_url()
        if (meta.get("custom") or {}).get("code:codeRepository") == repo_url:
            return True
        for ri in meta.get("related_identifiers", []):
            if repo_url in ri.get("identifier", ""):
                return True
        return False

    def get_versions(self) -> list[str]:
        """Return version strings from Zenodo records (cached)."""
        if self._cache is not None:
            return self._cache
        url = f"{self.BASE_URL}/records"
        # Omit https:// — the full URL causes Zenodo 500s on some repo names.
        params = {
            "q": f"related.identifier:github.com/{self.owner}/{self.repo}",
            "all_versions": "true",
        }
        client = self._client or httpx.Client()
        try:
            resp = client.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            self._cache = [
                v
                for hit in hits
                if self._hit_belongs_to_repo(hit)
                and (v := hit.get("metadata", {}).get("version"))
            ]
            return self._cache
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            self._cache = []
            return self._cache
        finally:
            if self._client is None:
                client.close()


class VersionMatcher:
    """Matches GitHub releases with Zenodo versions."""

    def match(
        self,
        github_releases: list[str],
        zenodo_versions: list[str],
    ) -> dict[str, Any]:
        """Return matching results between two version lists."""
        norm_github = [_normalize(v) for v in github_releases]
        norm_zenodo = {_normalize(v) for v in zenodo_versions}

        missing = [v for v in norm_github if v not in norm_zenodo]

        parsed_github = [p for v in norm_github if (p := _parse(v)) is not None]
        parsed_zenodo = [p for v in norm_zenodo if (p := _parse(v)) is not None]

        latest_github = max(parsed_github) if parsed_github else None
        latest_zenodo = max(parsed_zenodo) if parsed_zenodo else None

        is_behind = bool(
            latest_github and latest_zenodo and latest_github > latest_zenodo
        )

        return {
            "missing_versions": missing,
            "latest_github": str(latest_github) if latest_github else None,
            "latest_zenodo": str(latest_zenodo) if latest_zenodo else None,
            "is_behind": is_behind,
        }


class DriftEngine:
    """Detects release drift between GitHub and Zenodo."""

    def __init__(
        self,
        github_collector: GitHubCollector | None = None,
        zenodo_collector: ZenodoCollector | None = None,
    ) -> None:
        self._github = github_collector
        self._zenodo = zenodo_collector
        self._matcher = VersionMatcher()

    def detect(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """Return findings for the given repository."""
        github = self._github or GitHubCollector(owner, repo)
        zenodo = self._zenodo or ZenodoCollector(owner, repo)

        results = self._matcher.match(github.get_releases(), zenodo.get_versions())

        findings: list[dict[str, Any]] = []

        for version in results["missing_versions"]:
            findings.append(
                {
                    "code": "ZRD001",
                    "severity": "high",
                    "message": (
                        f"GitHub release {version} has no matching Zenodo archive."
                    ),
                    "version": version,
                }
            )

        if results["is_behind"]:
            findings.append(
                {
                    "code": "ZRD002",
                    "severity": "high",
                    "message": (
                        f"Latest Zenodo version is {results['latest_zenodo']}"
                        f" while latest GitHub release is"
                        f" {results['latest_github']}."
                    ),
                    "latest_github": results["latest_github"],
                    "latest_zenodo": results["latest_zenodo"],
                }
            )

        return findings


class GitHubUserCollector:
    """Lists repos owned by a GitHub user or org."""

    def __init__(self, username: str, client: Github | None = None) -> None:
        self.username = username
        self._client = client

    def get_repos(self) -> list[str]:
        """Return repo names owned by the user."""
        gh = self._client or _github_client()
        try:
            return [r.name for r in gh.get_user(self.username).get_repos(type="owner")]
        except GithubException as exc:
            status = exc.status if hasattr(exc, "status") else 0
            if status == 401:  # noqa: PLR2004
                raise RuntimeError(
                    f"GitHub API 401 for '{self.username}'. Check your GITHUB_TOKEN."
                ) from exc
            if status == 403:  # noqa: PLR2004
                raise RuntimeError(
                    f"GitHub API 403 for '{self.username}'."
                    " Set GITHUB_TOKEN to avoid rate limits."
                ) from exc
            raise RuntimeError(
                f"GitHub API error for '{self.username}': {exc}"
            ) from exc


class CheckUserResult:
    """Result of check_user, including counts for meaningful CLI output."""

    def __init__(
        self,
        findings: dict[str, list[dict[str, Any]]],
        repos_total: int,
        repos_with_zenodo: int,
    ) -> None:
        self.findings = findings
        self.repos_total = repos_total
        self.repos_with_zenodo = repos_with_zenodo


def check_user(
    username: str,
    github_user_collector: GitHubUserCollector | None = None,
) -> CheckUserResult:
    """Return drift findings for Zenodo-integrated repos owned by a GitHub user."""
    collector = github_user_collector or GitHubUserCollector(username)
    repos = collector.get_repos()
    findings: dict[str, list[dict[str, Any]]] = {}
    repos_with_zenodo = 0
    for repo_name in repos:
        zenodo = ZenodoCollector(username, repo_name)
        if not zenodo.get_versions():
            continue
        repos_with_zenodo += 1
        repo_findings = lint_repo(username, repo_name, zenodo_collector=zenodo)
        if repo_findings:
            findings[f"{username}/{repo_name}"] = repo_findings
    return CheckUserResult(findings, len(repos), repos_with_zenodo)


def explain_finding(finding: dict[str, Any]) -> str:
    """Return a human-readable explanation for a finding."""
    if finding["code"] == "ZRD001":
        return (
            f"GitHub release {finding['version']} exists but no matching"
            " Zenodo archive was found. This may indicate that the"
            " GitHub-Zenodo integration was disabled or failed during"
            " release processing."
        )
    if finding["code"] == "ZRD002":
        return (
            f"The latest Zenodo version ({finding['latest_zenodo']}) is"
            f" behind the latest GitHub release ({finding['latest_github']})."
            " This indicates that the repository has been updated on GitHub"
            " but not yet archived on Zenodo."
        )
    return "Unknown finding type."


def lint_repo(
    owner: str,
    repo: str,
    github_collector: GitHubCollector | None = None,
    zenodo_collector: ZenodoCollector | None = None,
) -> list[dict[str, Any]]:
    """Lint a repository for Zenodo release drift."""
    engine = DriftEngine(github_collector, zenodo_collector)
    return engine.detect(owner, repo)


def lint_repo_explain(
    owner: str,
    repo: str,
    github_collector: GitHubCollector | None = None,
    zenodo_collector: ZenodoCollector | None = None,
) -> str:
    """Lint a repository and return a Markdown explanation report."""
    findings = lint_repo(owner, repo, github_collector, zenodo_collector)

    if not findings:
        return f"# Repository: {owner}/{repo}\n\nNo drift detected."

    lines = [f"# Repository: {owner}/{repo}\n"]
    for finding in findings:
        lines.append(f"## {finding['code']} {finding['severity'].upper()}")
        lines.append(finding["message"])
        lines.append("")
        lines.append(f"> {explain_finding(finding)}")
        lines.append("")

    return "\n".join(lines)


class ZenodoUploader:
    """Uploads GitHub releases to Zenodo.

    When existing Zenodo records are found for the repository, each upload is
    created as a new version of the existing concept record so that all
    versions share the same concept DOI. When no existing record exists, a
    fresh deposition (and concept) is created.
    """

    def __init__(
        self,
        token: str,
        sandbox: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.base_url = ZENODO_SANDBOX_BASE_URL if sandbox else ZENODO_BASE_URL
        self._client = client
        self._owns_client = client is None

    def _make_client(self) -> httpx.Client:
        return httpx.Client(
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def _metadata(self, owner: str, repo: str, version: str) -> dict[str, Any]:
        return {
            "title": f"{repo} {version}",
            "upload_type": "software",
            "description": (
                f"Source code archive for {owner}/{repo} release {version}."
            ),
            "version": version,
            "related_identifiers": [
                {
                    "relation": "isSupplementTo",
                    "identifier": f"https://github.com/{owner}/{repo}",
                    "resource_type": "software",
                    "scheme": "url",
                }
            ],
        }

    def _find_latest_record_id(
        self, client: httpx.Client, owner: str, repo: str
    ) -> int | None:
        """Return the Zenodo record ID for the latest published version, or None."""
        params = {
            "q": f"related.identifier:github.com/{owner}/{repo}",
            "sort": "mostrecent",
            "size": 1,
        }
        resp = client.get(f"{self.base_url}/records", params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:  # noqa: PLR2004
            return None
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            return None
        return hits[0].get("id")

    def _new_deposition(
        self, client: httpx.Client, owner: str, repo: str, version: str
    ) -> dict[str, Any]:
        resp = client.post(
            f"{self.base_url}/depositions",
            json={"metadata": self._metadata(owner, repo, version)},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _new_version(
        self,
        client: httpx.Client,
        record_id: int,
        owner: str,
        repo: str,
        version: str,
    ) -> dict[str, Any]:
        """Branch a new draft version from an existing concept record."""
        resp = client.post(
            f"{self.base_url}/depositions/{record_id}/actions/newversion",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        draft_url = resp.json()["links"]["latest_draft"]
        draft_resp = client.get(draft_url, timeout=_TIMEOUT)
        draft_resp.raise_for_status()
        draft = draft_resp.json()
        draft_id = draft["id"]

        # Remove files copied from the parent version before we upload ours.
        for f in draft.get("files", []):
            client.delete(
                f"{self.base_url}/depositions/{draft_id}/files/{f['id']}",
                timeout=_TIMEOUT,
            )

        # Update metadata (version string, title, etc.)
        client.put(
            f"{self.base_url}/depositions/{draft_id}",
            json={"metadata": self._metadata(owner, repo, version)},
            timeout=_TIMEOUT,
        ).raise_for_status()

        return draft

    def _upload_file(
        self,
        client: httpx.Client,
        bucket_url: str,
        owner: str,
        repo: str,
        tag: str,
    ) -> None:
        archive_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip"
        filename = f"{repo}-{tag}.zip"
        with httpx.stream(
            "GET", archive_url, follow_redirects=True, timeout=_UPLOAD_TIMEOUT
        ) as stream:
            stream.raise_for_status()
            client.put(
                f"{bucket_url}/{filename}",
                content=stream.iter_bytes(),
                timeout=_UPLOAD_TIMEOUT,
            ).raise_for_status()

    def _publish(self, client: httpx.Client, deposition_id: int) -> dict[str, Any]:
        resp = client.post(
            f"{self.base_url}/depositions/{deposition_id}/actions/publish",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def upload_release(
        self,
        owner: str,
        repo: str,
        tag: str,
        version: str,
    ) -> dict[str, Any]:
        """Upload and publish a GitHub release tag to Zenodo.

        If an existing Zenodo record for the repository is found, the upload is
        created as a new version under the same concept DOI. Otherwise a new
        concept is created.
        """
        client = self._client or self._make_client()
        try:
            record_id = self._find_latest_record_id(client, owner, repo)
            if record_id is not None:
                deposition = self._new_version(client, record_id, owner, repo, version)
            else:
                deposition = self._new_deposition(client, owner, repo, version)

            self._upload_file(client, deposition["links"]["bucket"], owner, repo, tag)
            published = self._publish(client, deposition["id"])
            return {
                "version": version,
                "tag": tag,
                "concept_doi": published.get("conceptdoi"),
                "doi": published.get("doi"),
                "zenodo_url": published.get("links", {}).get("html"),
                "status": "published",
            }
        except httpx.HTTPStatusError as exc:
            result: dict[str, Any] = {
                "version": version,
                "tag": tag,
                "status": "error",
                "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            }
            if exc.response.status_code == 403:  # noqa: PLR2004
                result["hint"] = (
                    "403 Forbidden: your ZENODO_TOKEN does not have edit access to"
                    " the existing Zenodo record for this repository. This happens"
                    " when the original records were created by the GitHub-Zenodo"
                    " webhook under a different Zenodo account. To fix this, log in"
                    " to Zenodo as the record owner, open the record, and use"
                    " 'Edit > Share' to grant your account curator access — or"
                    " transfer ownership via the Zenodo support team."
                )
            return result
        except httpx.RequestError as exc:
            return {
                "version": version,
                "tag": tag,
                "status": "error",
                "error": str(exc),
            }
        finally:
            if self._owns_client:
                client.close()


def _filter_by_range(
    versions: list[str],
    from_version: str | None,
    to_version: str | None,
) -> list[str]:
    lo = _parse(_normalize(from_version)) if from_version else None
    hi = _parse(_normalize(to_version)) if to_version else None
    result = []
    for v in versions:
        p = _parse(v)
        if p is None:
            continue
        if lo is not None and p < lo:
            continue
        if hi is not None and p > hi:
            continue
        result.append(v)
    return result


def fix_repo(  # noqa: PLR0913
    owner: str,
    repo: str,
    token: str,
    version: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    sandbox: bool = False,
    github_collector: GitHubCollector | None = None,
    zenodo_collector: ZenodoCollector | None = None,
) -> list[dict[str, Any]]:
    """Upload missing GitHub releases to Zenodo.

    If *version* is given, only that version is uploaded.
    Otherwise every version reported as missing by drift detection is uploaded,
    optionally filtered to the semver range [from_version, to_version] inclusive.
    Non-parseable versions are excluded when a range is active.
    """
    uploader = ZenodoUploader(token=token, sandbox=sandbox)
    results: list[dict[str, Any]] = []

    gh_collector = github_collector or GitHubCollector(owner, repo)
    releases = gh_collector.get_releases()

    if version:
        # Find the matching tag (may have a 'v' prefix or not).
        norm_target = _normalize(version)
        tag = next(
            (r for r in releases if _normalize(r) == norm_target),
            None,
        )
        if tag is None:
            return [
                {
                    "version": version,
                    "tag": None,
                    "status": "error",
                    "error": f"No GitHub release found matching version '{version}'.",
                }
            ]
        results.append(
            uploader.upload_release(owner, repo, tag=tag, version=norm_target)
        )
    else:
        zen_collector = zenodo_collector or ZenodoCollector(owner, repo)
        findings = lint_repo(owner, repo, gh_collector, zen_collector)
        missing = [f["version"] for f in findings if f["code"] == "ZRD001"]
        if not missing:
            return []
        # Upload oldest-first so that each newversion call branches from the
        # correct predecessor and Zenodo's version chain reflects semver order.
        # Versions that cannot be parsed as semver are appended after the rest.
        missing.sort(key=lambda v: (_parse(v) is None, _parse(v) or v))

        if from_version is not None or to_version is not None:
            missing = _filter_by_range(missing, from_version, to_version)

        norm_releases = {_normalize(r): r for r in releases}
        for ver in missing:
            tag = norm_releases.get(ver)
            if tag is None:
                results.append(
                    {
                        "version": ver,
                        "tag": None,
                        "status": "error",
                        "error": f"Tag for version '{ver}' not found.",
                    }
                )
                continue
            results.append(uploader.upload_release(owner, repo, tag=tag, version=ver))

    return results
