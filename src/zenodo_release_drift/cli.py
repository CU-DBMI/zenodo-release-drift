"""
CLI for zenodo-release-drift.
"""

from __future__ import annotations

import json
import os
import textwrap
from typing import Any

import typer
from tabulate import tabulate

try:
    from zenodo_release_drift._version import __version__
except ImportError:
    __version__ = "unknown"
from zenodo_release_drift.main import (
    check_user,
    fix_repo,
    lint_repo,
    lint_repo_explain,
)

app = typer.Typer()

_TABLE_FMT = "simple"
_VERSION_WRAP = 60

_CODE_DESCRIPTION = {
    "ZRD001": "Release(s) not archived",
    "ZRD002": "Zenodo out of date",
}


def _findings_to_rows(findings: list[dict[str, Any]]) -> list[list[str]]:
    """Collapse findings into one table row per check code."""
    rows: list[list[str]] = []

    zrd001 = [f for f in findings if f["code"] == "ZRD001"]
    if zrd001:
        versions = ", ".join(f["version"] for f in zrd001)
        wrapped = textwrap.fill(versions, width=_VERSION_WRAP)
        detail = f"{len(zrd001)} release(s) not archived:\n{wrapped}"
        rows.append(["ZRD001", _CODE_DESCRIPTION["ZRD001"], detail])

    for finding in findings:
        if finding["code"] == "ZRD002":
            detail = (
                f"Zenodo latest: {finding['latest_zenodo']}"
                f"  |  GitHub latest: {finding['latest_github']}"
            )
            rows.append(["ZRD002", _CODE_DESCRIPTION["ZRD002"], detail])

    return rows


def _format_terminal(repo: str, findings: list[dict[str, Any]]) -> str:
    lines = [f"Repository: {repo}"]
    if not findings:
        lines.append("  No drift detected.")
        return "\n".join(lines)
    rows = _findings_to_rows(findings)
    lines.append(
        tabulate(
            rows,
            headers=["Code", "Description", "Details"],
            tablefmt=_TABLE_FMT,
        )
    )
    return "\n".join(lines)


def _format_markdown(repo: str, findings: list[dict[str, Any]]) -> str:
    if not findings:
        return f"# Repository: {repo}\n\nNo drift detected."
    lines = [f"# Repository: {repo}\n"]
    rows = _findings_to_rows(findings)
    lines.append(
        tabulate(
            rows,
            headers=["Code", "Description", "Details"],
            tablefmt="github",
        )
    )
    lines.append("")
    return "\n".join(lines)


@app.command()
def lint(
    repo: str = typer.Argument(..., help="Repository owner/repo"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    markdown_output: bool = typer.Option(
        False, "--markdown", help="Output as Markdown"
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Human-readable explanations"
    ),
) -> None:
    """Lint a repository for Zenodo release drift."""
    if "/" not in repo:
        typer.echo("Error: Repository must be in the format 'owner/repo'")
        raise typer.Exit(code=1)

    owner, repo_name = repo.split("/", 1)

    if explain:
        typer.echo(lint_repo_explain(owner, repo_name))
        return

    findings = lint_repo(owner, repo_name)

    if json_output:
        typer.echo(json.dumps(findings, indent=2))
    elif markdown_output:
        typer.echo(_format_markdown(repo, findings))
    else:
        typer.echo(_format_terminal(repo, findings))

    if findings:
        raise typer.Exit(code=1)


def _check_single_repo(
    target: str,
    json_output: bool,
    markdown_output: bool,
    explain: bool,
) -> None:
    owner, repo_name = target.split("/", 1)
    if explain:
        typer.echo(lint_repo_explain(owner, repo_name))
        return
    findings = lint_repo(owner, repo_name)
    if json_output:
        typer.echo(json.dumps(findings, indent=2))
    elif markdown_output:
        typer.echo(_format_markdown(target, findings))
    else:
        typer.echo(_format_terminal(target, findings))
    if findings:
        raise typer.Exit(code=1)


def _check_user_account(target: str, json_output: bool) -> None:
    try:
        result = check_user(target)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(result.findings, indent=2))
        return

    typer.echo(f"GitHub user or org: {target}")
    typer.echo(
        f"{result.repos_total} repos found,"
        f" {result.repos_with_zenodo} with Zenodo integration."
    )
    typer.echo("")

    if not result.findings:
        typer.echo("No drift detected.")
        return

    all_rows: list[list[str]] = []
    for repo, findings in result.findings.items():
        for row in _findings_to_rows(findings):
            all_rows.append([repo, *row])

    typer.echo(
        tabulate(
            all_rows,
            headers=["Repository", "Code", "Description", "Details"],
            tablefmt=_TABLE_FMT,
        )
    )


@app.command()
def check(
    target: str = typer.Argument(
        ..., help="GitHub username/org, or owner/repo for a single repo"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    markdown_output: bool = typer.Option(
        False, "--markdown", help="Output as Markdown (single repo only)"
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Human-readable explanations (single repo only)"
    ),
) -> None:
    """Check for Zenodo release drift.

    Pass owner/repo to check a single repository, or a GitHub username/org
    to scan all their repositories.
    """
    if "/" in target:
        _check_single_repo(target, json_output, markdown_output, explain)
    else:
        _check_user_account(target, json_output)


def _print_fix_results(results: list[dict[str, Any]]) -> None:
    if not results:
        typer.echo("No missing versions to upload.")
        return
    for result in results:
        ver = result["version"]
        if result["status"] == "published":
            doi = result.get("doi") or "—"
            concept = result.get("concept_doi")
            url = result.get("zenodo_url") or ""
            concept_part = f"  concept:{concept}" if concept else ""
            typer.echo(f"  [OK] {ver}  doi:{doi}{concept_part}  {url}")
        else:
            typer.echo(f"  [ERROR] {ver}: {result.get('error', 'unknown error')}")
            if hint := result.get("hint"):
                typer.echo(f"\n  Hint: {hint}\n")


@app.command()
def fix(  # noqa: PLR0913
    repo: str = typer.Argument(..., help="Repository in owner/repo format"),
    version_filter: str | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Upload only this specific version (default: all missing versions)",
    ),
    from_version: str | None = typer.Option(
        None,
        "--from",
        help="Only upload versions at or above this semver (inclusive)",
    ),
    to_version: str | None = typer.Option(
        None,
        "--to",
        help="Only upload versions at or below this semver (inclusive)",
    ),
    sandbox: bool = typer.Option(
        False,
        "--sandbox",
        help="Use Zenodo sandbox instead of production",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """Upload missing GitHub releases to Zenodo.

    Reads ZENODO_TOKEN from the environment. Use --sandbox for testing
    against sandbox.zenodo.org without publishing to production.

    Pass --version to upload a single specific release; omit it to upload
    every release currently missing from Zenodo. Use --from and --to to
    restrict uploads to a semver range (both bounds are inclusive).
    """
    if "/" not in repo:
        typer.echo("Error: Repository must be in the format 'owner/repo'", err=True)
        raise typer.Exit(code=1)

    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        typer.echo(
            "Error: ZENODO_TOKEN environment variable is not set.",
            err=True,
        )
        raise typer.Exit(code=1)

    owner, repo_name = repo.split("/", 1)

    if not json_output:
        env_label = "sandbox" if sandbox else "production"
        typer.echo(f"Uploading to Zenodo {env_label}: {repo}")
        if version_filter:
            typer.echo(f"  Version: {version_filter}")
        else:
            range_parts = []
            if from_version:
                range_parts.append(f">= {from_version}")
            if to_version:
                range_parts.append(f"<= {to_version}")
            range_label = f" ({', '.join(range_parts)})" if range_parts else ""
            typer.echo(f"  Scanning for all missing versions{range_label}…")
        typer.echo(
            "  Note: each archive is fetched from GitHub's tag endpoint at"
            " the time of upload and reflects the current state of the tag."
            " For most repositories this is identical to the original release."
        )

    results = fix_repo(
        owner,
        repo_name,
        token=token,
        version=version_filter,
        from_version=from_version,
        to_version=to_version,
        sandbox=sandbox,
    )

    if json_output:
        typer.echo(json.dumps(results, indent=2))
    else:
        _print_fix_results(results)

    if any(r["status"] == "error" for r in results):
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Show the version of zenodo-release-drift."""
    typer.echo(f"zenodo-release-drift v{__version__}")


def trigger() -> None:
    """Trigger the CLI."""
    app()
