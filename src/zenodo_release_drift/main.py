"""
Core logic for zenodo-release-drift.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from typing import Any

import httpx
import yaml
from github import Auth, Github, GithubException
from packaging.version import InvalidVersion, Version

_TIMEOUT = httpx.Timeout(10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(3600.0)

ZENODO_BASE_URL = "https://zenodo.org/api"
ZENODO_SANDBOX_BASE_URL = "https://sandbox.zenodo.org/api"
_DEPOSIT_PATH = "deposit/depositions"


def _fetch_citation_cff(owner: str, repo: str) -> dict[str, Any] | None:
    """Fetch and parse CITATION.cff from the default branch of a GitHub repo."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/CITATION.cff"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:  # noqa: PLR2004
            return None
        return yaml.safe_load(resp.text)  # type: ignore[no-any-return]
    except Exception:
        return None


def _creators_from_citation(cff: dict[str, Any]) -> list[dict[str, str]]:
    """Convert CITATION.cff authors list to Zenodo creators format."""
    creators = []
    for author in cff.get("authors", []):
        family = author.get("family-names", "")
        given = author.get("given-names", "")
        name = f"{family}, {given}".strip(", ") or author.get("name", "")
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if orcid := author.get("orcid", ""):
            entry["orcid"] = str(orcid).replace("https://orcid.org/", "")
        if affiliation := author.get("affiliation", ""):
            entry["affiliation"] = str(affiliation)
        creators.append(entry)
    return creators


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

    def get_release_dates(self) -> dict[str, str]:
        """Return a mapping of tag name → ISO date string (YYYY-MM-DD)."""
        gh = self._client or _github_client()
        try:
            gh_repo = gh.get_repo(f"{self.owner}/{self.repo}")
            return {
                r.tag_name: r.published_at.strftime("%Y-%m-%d")
                for r in gh_repo.get_releases()
                if r.published_at is not None
            }
        except GithubException:
            return {}


class ZenodoCollector:
    """Collects Zenodo records for a GitHub repository."""

    BASE_URL = "https://zenodo.org/api"

    def __init__(
        self, owner: str, repo: str, client: httpx.Client | None = None
    ) -> None:
        # GitHub repo names are case-insensitive; Zenodo stores whatever casing
        # was used at archive time. Normalising to lowercase here ensures the
        # search query and URL comparison match regardless of how the caller
        # supplied the owner/repo.
        self.owner = owner.lower()
        self.repo = repo.lower()
        self._client = client
        self._cache: list[str] | None = None

    def _repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def _hit_belongs_to_repo(self, hit: dict[str, Any]) -> bool:
        """Return True if the Zenodo record is linked to this GitHub repo.

        Comparison is case-insensitive: ``_repo_url()`` is already lowercased,
        and Zenodo stores whatever casing the repo used at archive time (e.g.
        ``.../cytomining/CytoTable``), so the record values are lowercased here
        before comparing.
        """
        meta = hit.get("metadata", {})
        repo_url = self._repo_url()
        code_repo = (meta.get("custom") or {}).get("code:codeRepository") or ""
        if code_repo.lower() == repo_url:
            return True
        for ri in meta.get("related_identifiers", []):
            if repo_url in (ri.get("identifier") or "").lower():
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

    def _base_metadata(
        self,
        owner: str,
        repo: str,
        version: str,
        cff: dict[str, Any] | None = None,
        publication_date: str | None = None,
    ) -> dict[str, Any]:
        title = f"{repo} {version}"
        description = f"Source code archive for {owner}/{repo} release {version}."
        if cff:
            cff_title = cff.get("title")
            if cff_title:
                title = f"{cff_title} {version}"
            cff_abstract = cff.get("abstract")
            if cff_abstract:
                description = str(cff_abstract)
        meta: dict[str, Any] = {
            "title": title,
            "upload_type": "software",
            "description": description,
            "version": version,
        }
        if publication_date:
            meta["publication_date"] = publication_date
        meta["related_identifiers"] = [
            {
                "relation": "isSupplementTo",
                "identifier": f"https://github.com/{owner}/{repo}",
                "resource_type": "software",
                "scheme": "url",
            }
        ]
        if cff:
            creators = _creators_from_citation(cff)
            if creators:
                meta["creators"] = creators
            if message := cff.get("message"):
                meta["notes"] = str(message)
            if keywords := cff.get("keywords"):
                meta["keywords"] = [str(k) for k in keywords]
            if license_id := cff.get("license"):
                meta["license"] = str(license_id)
        return meta

    def _new_deposition_metadata(
        self,
        owner: str,
        repo: str,
        version: str,
        cff: dict[str, Any] | None = None,
        publication_date: str | None = None,
    ) -> dict[str, Any]:
        meta = self._base_metadata(owner, repo, version, cff, publication_date)
        if "creators" not in meta:
            meta["creators"] = [{"name": owner}]
        return meta

    def _find_records_by_concept_doi(
        self,
        client: httpx.Client,
        concept_doi: str,
    ) -> list[tuple[int, str]]:
        """Return the record ID derived from *concept_doi* as the sole candidate.

        Zenodo concept DOIs have the form ``10.5281/zenodo.{id}``.  The numeric
        ID at the end is the concept record ID; passing it to the ``newversion``
        action creates a new draft under that concept without any search round-trip.
        The source label is ``"concept-doi"`` for diagnostics.
        """
        try:
            record_id = int(concept_doi.rsplit(".", 1)[-1])
        except ValueError:
            return []
        return [(record_id, "concept-doi")]

    def _find_candidate_record_ids(
        self, client: httpx.Client, owner: str, repo: str
    ) -> list[tuple[int, str]]:
        """Return candidate (id, source) pairs to try for newversion.

        Returns up to 10 candidates in preference order:
        - Public records sorted oldest-first: original webhook-created records
          have long-registered DOIs and are the right target for newversion.
        - Authenticated depositions sorted most-recent: the user's own records,
          listed after the public ones so recently-created standalones (whose
          DOIs may not yet be registered) are tried last.

        The same numeric ID is valid for both the public records and deposit
        APIs; any of them can serve as the parent for a newversion call when
        the caller has edit/curator access to the record.
        """
        q = f"related.identifier:github.com/{owner}/{repo}"
        candidates: list[tuple[int, str]] = []
        seen: set[int] = set()

        # Oldest published records first — original webhook records are here
        # and their DOIs are long-registered, making newversion reliable.
        pub_resp = client.get(
            f"{self.base_url}/records",
            params={"q": q, "sort": "-mostrecent", "size": 5},
            timeout=_TIMEOUT,
        )
        if pub_resp.status_code == 200:  # noqa: PLR2004
            for hit in pub_resp.json().get("hits", {}).get("hits", []):
                rid = hit.get("id")
                if rid and rid not in seen:
                    candidates.append((rid, "public-oldest"))
                    seen.add(rid)

        # Most-recent depositions owned by the authenticated user (may include
        # recently-created standalones whose DOIs are not yet registered).
        dep_resp = client.get(
            f"{self.base_url}/{_DEPOSIT_PATH}",
            params={"q": q, "sort": "mostrecent", "size": 5, "status": "published"},
            timeout=_TIMEOUT,
        )
        if dep_resp.status_code == 200:  # noqa: PLR2004
            for hit in dep_resp.json():
                rid = hit.get("id")
                if rid and rid not in seen:
                    candidates.append((rid, "depositions"))
                    seen.add(rid)

        return candidates

    def _new_deposition(  # noqa: PLR0913, PLR0917
        self,
        client: httpx.Client,
        owner: str,
        repo: str,
        version: str,
        cff: dict[str, Any] | None = None,
        publication_date: str | None = None,
    ) -> dict[str, Any]:
        meta = self._new_deposition_metadata(
            owner, repo, version, cff, publication_date
        )
        resp = client.post(
            f"{self.base_url}/{_DEPOSIT_PATH}",
            json={"metadata": meta},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _new_version(  # noqa: PLR0913, PLR0917
        self,
        client: httpx.Client,
        record_id: int,
        owner: str,
        repo: str,
        version: str,
        cff: dict[str, Any] | None = None,
        publication_date: str | None = None,
    ) -> dict[str, Any]:
        """Branch a new draft version from an existing concept record."""
        resp = client.post(
            f"{self.base_url}/{_DEPOSIT_PATH}/{record_id}/actions/newversion",
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
                f"{self.base_url}/{_DEPOSIT_PATH}/{draft_id}/files/{f['id']}",
                timeout=_TIMEOUT,
            )

        # Merge our fields into the inherited metadata.
        # CITATION.cff fields (creators, title, description) take precedence so
        # the record reflects the current state of the project's citation info.
        # Strip read-only and format-incompatible fields that Zenodo rejects on PUT:
        # - doi/recid/concept* are server-managed
        # - dates can have a legacy format the current API validation rejects
        _STRIP_INHERITED = {
            "doi",
            "prereserve_doi",
            "recid",
            "conceptdoi",
            "conceptrecid",
            "dates",
        }
        inherited = {
            k: v
            for k, v in draft.get("metadata", {}).items()
            if k not in _STRIP_INHERITED
        }
        our_meta = self._base_metadata(owner, repo, version, cff, publication_date)
        merged = {**inherited, **our_meta}
        client.put(
            f"{self.base_url}/{_DEPOSIT_PATH}/{draft_id}",
            json={"metadata": merged},
            timeout=_TIMEOUT,
        ).raise_for_status()

        return draft

    def _upload_file(  # noqa: PLR0913, PLR0917
        self,
        client: httpx.Client,
        bucket_url: str,
        owner: str,
        repo: str,
        tag: str,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        archive_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip"
        filename = f"{repo}-{tag}.zip"

        # Stream download so callers can track progress.
        buf = bytearray()
        with (
            httpx.Client() as dl,
            dl.stream(
                "GET", archive_url, follow_redirects=True, timeout=_UPLOAD_TIMEOUT
            ) as resp,
        ):
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            for chunk in resp.iter_bytes(chunk_size=65536):
                buf += chunk
                if on_progress:
                    on_progress("download", len(buf), total)
        if on_progress:
            on_progress("download", len(buf), len(buf))  # mark 100% complete

        # Stream upload from the buffer so callers can track progress.
        uploaded = 0
        total = len(buf)

        def _iter_buf() -> Generator[bytes, None, None]:
            nonlocal uploaded
            view = memoryview(buf)
            chunk_size = 65536
            offset = 0
            while offset < total:
                chunk = bytes(view[offset : offset + chunk_size])
                uploaded += len(chunk)
                if on_progress:
                    on_progress("upload", uploaded, total)
                offset += chunk_size
                yield chunk  # type: ignore[misc]

        client.put(
            f"{bucket_url}/{filename}",
            content=_iter_buf(),
            headers={"Content-Length": str(total)},
            timeout=_UPLOAD_TIMEOUT,
        ).raise_for_status()

    def _publish(self, client: httpx.Client, deposition_id: int) -> dict[str, Any]:
        resp = client.post(
            f"{self.base_url}/{_DEPOSIT_PATH}/{deposition_id}/actions/publish",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _resolve_deposition(  # noqa: PLR0913, PLR0917
        self,
        client: httpx.Client,
        owner: str,
        repo: str,
        version: str,
        cff: dict[str, Any] | None,
        publication_date: str | None,
        concept_doi: str | None,
        diag: list[str],
    ) -> tuple[dict[str, Any], bool]:
        """Return ``(deposition_draft, new_concept)`` for the upload.

        Tries to branch a new version off an existing record (the first
        candidate that accepts the ``newversion`` action wins). Falls back to a
        new standalone concept if no candidate accepts it. Diagnostic lines are
        appended to *diag*.
        """
        if concept_doi:
            candidates = self._find_records_by_concept_doi(client, concept_doi)
            diag.append(f"concept-doi override: {concept_doi}")
        else:
            candidates = self._find_candidate_record_ids(client, owner, repo)
        diag.append(f"candidates: {[(rid, src) for rid, src in candidates]}")

        for record_id, id_source in candidates:
            try:
                deposition = self._new_version(
                    client, record_id, owner, repo, version, cff, publication_date
                )
                diag.append(
                    f"newversion: ok id={record_id} source={id_source}"
                    f" draft={deposition['id']}"
                )
                return deposition, False
            except httpx.HTTPStatusError as nv_exc:
                diag.append(
                    f"newversion: skip id={record_id} source={id_source}"
                    f" — HTTP {nv_exc.response.status_code}:"
                    f" {nv_exc.response.text[:120]}"
                )

        diag.append("newversion: all candidates failed, creating new concept")
        deposition = self._new_deposition(
            client, owner, repo, version, cff, publication_date
        )
        return deposition, True

    def upload_release(  # noqa: PLR0913, PLR0917
        self,
        owner: str,
        repo: str,
        tag: str,
        version: str,
        concept_doi: str | None = None,
        publication_date: str | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Upload and publish a GitHub release tag to Zenodo.

        If *concept_doi* is given, the upload is linked under that specific
        concept DOI rather than the one discovered automatically.  Otherwise
        the tool searches for an existing Zenodo record associated with the
        repository and uses that (falling back to a new standalone deposition).

        *publication_date* (``YYYY-MM-DD``) sets the Zenodo publication date so
        that versions are ordered by their original GitHub release date rather
        than by the time the upload ran.
        """
        client = self._client or self._make_client()
        cff = _fetch_citation_cff(owner, repo)
        diag: list[str] = [f"CITATION.cff: {'found' if cff else 'not found'}"]
        if publication_date:
            diag.append(f"publication_date: {publication_date}")
        try:
            deposition, new_concept = self._resolve_deposition(
                client, owner, repo, version, cff, publication_date, concept_doi, diag
            )

            self._upload_file(
                client, deposition["links"]["bucket"], owner, repo, tag, on_progress
            )
            published = self._publish(client, deposition["id"])
            result: dict[str, Any] = {
                "version": version,
                "tag": tag,
                "concept_doi": published.get("conceptdoi"),
                "doi": published.get("doi"),
                "zenodo_url": published.get("links", {}).get("html"),
                "status": "published",
                "diag": diag,
            }
            if new_concept:
                result["warning"] = (
                    "Could not add this upload as a new version of any existing"
                    " Zenodo record. A standalone deposition was created instead."
                    " Run with --verbose to see each candidate tried."
                    " Contact Zenodo support to have it merged into the existing"
                    " concept record if needed."
                )
            return result
        except httpx.HTTPStatusError as exc:
            result: dict[str, Any] = {
                "version": version,
                "tag": tag,
                "status": "error",
                "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            }
            if exc.response.status_code in (403, 404):
                result["hint"] = (
                    f"HTTP {exc.response.status_code}: your ZENODO_TOKEN does not"
                    " have edit access to the existing Zenodo record for this"
                    " repository. This happens when the original records were"
                    " created by the GitHub-Zenodo webhook under a different Zenodo"
                    " account — Zenodo returns 404 for records owned by another"
                    " account and 403 when access is explicitly denied. To fix"
                    " this, log in to Zenodo as the record owner, open the record,"
                    " and use 'Edit > Share' to grant your account curator access"
                    " — or transfer ownership via the Zenodo support team."
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


def _latest_version(versions: list[str]) -> Version | None:
    """Return the highest parseable semver in *versions*, or None if none parse."""
    parsed = [p for v in versions if (p := _parse(_normalize(v))) is not None]
    return max(parsed) if parsed else None


def _filter_since_latest(
    versions: list[str],
    zenodo_versions: list[str],
) -> list[str]:
    """Keep only versions strictly newer than the latest Zenodo archive.

    If Zenodo has no parseable version yet, every candidate is returned (there
    is no floor to scan from). Non-parseable candidates are dropped.
    """
    floor = _latest_version(zenodo_versions)
    if floor is None:
        return versions
    return [v for v in versions if (p := _parse(v)) is not None and p > floor]


def fix_repo(  # noqa: PLR0913, PLR0917
    owner: str,
    repo: str,
    token: str,
    version: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    since_latest: bool = False,
    concept_doi: str | None = None,
    force: bool = False,
    sandbox: bool = False,
    github_collector: GitHubCollector | None = None,
    zenodo_collector: ZenodoCollector | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Upload missing GitHub releases to Zenodo.

    If *version* is given, only that version is uploaded. When that version is
    already archived on Zenodo it is skipped unless *force* is True (which
    uploads it again as an additional new version).
    Otherwise every version reported as missing by drift detection is uploaded,
    optionally filtered to the semver range [from_version, to_version] inclusive.
    Non-parseable versions are excluded when a range is active.

    When *since_latest* is True, only missing versions strictly newer than the
    latest version already on Zenodo are uploaded. This guarantees every upload
    appends to the head of the version chain and never reorders existing
    versions.
    """
    uploader = ZenodoUploader(token=token, sandbox=sandbox)
    results: list[dict[str, Any]] = []

    gh_collector = github_collector or GitHubCollector(owner, repo)
    releases = gh_collector.get_releases()
    release_dates = gh_collector.get_release_dates()

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
        # Guard against creating a duplicate: skip if this version is already
        # archived on Zenodo, unless the caller explicitly forces a re-upload.
        zen_collector = zenodo_collector or ZenodoCollector(owner, repo)
        existing = {_normalize(v) for v in zen_collector.get_versions()}
        if norm_target in existing and not force:
            return [
                {
                    "version": norm_target,
                    "tag": tag,
                    "status": "skipped",
                    "reason": (
                        f"Version {norm_target} is already archived on Zenodo."
                        " Use --force to upload it again as a new version."
                    ),
                }
            ]
        results.append(
            uploader.upload_release(
                owner,
                repo,
                tag=tag,
                version=norm_target,
                concept_doi=concept_doi,
                publication_date=release_dates.get(tag),
                on_progress=on_progress,
            )
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

        if since_latest:
            missing = _filter_since_latest(missing, zen_collector.get_versions())

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
            results.append(
                uploader.upload_release(
                    owner,
                    repo,
                    tag=tag,
                    version=ver,
                    concept_doi=concept_doi,
                    publication_date=release_dates.get(tag),
                    on_progress=on_progress,
                )
            )

    return results
