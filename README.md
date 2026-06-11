# zenodo-release-drift

Detect drift between GitHub releases and Zenodo archives.

When a repository is connected to Zenodo via the GitHub–Zenodo webhook, every new release should be automatically archived.
In practice the webhook silently fails, gets disabled, or simply falls behind.
`zenodo-release-drift` surfaces those gaps so you can act on them.

## Installation

```bash
pip install zenodo-release-drift
# or with uv
uv add zenodo-release-drift
```

## Quick start

Check a single repository:

```bash
zenodo-release-drift check owner/repo
```

Scan every repository owned by a GitHub user or org:

```bash
zenodo-release-drift check example-org
```

Example output:

```
GitHub user or org: example-org
28 repos found, 5 with Zenodo integration.

Repository                  Code    Description              Details
--------------------------  ------  -----------------------  -------------------------------------------
example-org/example-repo    ZRD001  Release(s) not archived  15 release(s) not archived:
                                                             1.2.0, 1.1.4, 1.0.1, ...
example-org/example-repo    ZRD002  Zenodo out of date       Zenodo latest: 1.1.3  |  GitHub latest: 1.2.0
```

Exit code is **1** when drift is found (single-repo mode), **0** when clean —
suitable for CI and pre-commit hooks.

## Commands

### `check`

```
zenodo-release-drift check [OPTIONS] TARGET
```

`TARGET` is either `owner/repo` (single repository) or a GitHub username/org
(scans all owned repositories).

| Option       | Description                                                        |
| ------------ | ------------------------------------------------------------------ |
| `--json`     | Output findings as JSON                                            |
| `--markdown` | Output as a Markdown table (single repo only)                      |
| `--explain`  | Full human-readable explanation of each finding (single repo only) |

### `lint`

Explicit single-repository check — useful in pre-commit hooks where the
command name should be unambiguous.

```
zenodo-release-drift lint [OPTIONS] OWNER/REPO
```

Same options as `check`.

### `fix`

Upload releases that are missing from Zenodo back to the archive.

```
zenodo-release-drift fix [OPTIONS] OWNER/REPO
```

Requires a `ZENODO_TOKEN` environment variable — a personal access token
created at <https://zenodo.org/account/settings/applications/>.

| Option      | Description                                                   |
| ----------- | ------------------------------------------------------------- |
| `--version` | Upload only this specific version (default: all missing ones) |
| `--from`    | Only upload versions at or above this semver (inclusive)      |
| `--to`      | Only upload versions at or below this semver (inclusive)      |
| `--sandbox` | Target `sandbox.zenodo.org` instead of production             |
| `--json`    | Output results as JSON                                        |

**How versions are grouped**: if Zenodo already holds records for the repository, each upload is created as a new version under the same concept DOI so all versions remain linked.
If no existing records are found, a new concept is created.

**Note on uploaded source content**: the archive uploaded for each version is fetched from GitHub's tag archive endpoint at the moment `fix` runs, and reflects where the tag points at that time.
For the vast majority of repositories this is identical to the original release — tags are not normally moved after publishing.
If a tag has been amended since the original release was made, the archive will reflect the current state of the tag rather than its historical state; this is a property of how git tags work rather than a limitation of this tool.

> **Important — record ownership**
>
> The `ZENODO_TOKEN` you supply must belong to the same Zenodo account that
> owns the existing records. When the original records were created by the
> GitHub–Zenodo webhook they are owned by whichever Zenodo account connected
> the webhook — not necessarily yours. If your token belongs to a different
> account the `newversion` API call will return HTTP 403 and the upload will
> fail with a clear hint message.
>
> **To resolve a 403:**
>
> 1. Log in to Zenodo as the record owner.
> 1. Open the existing record and go to **Edit → Share**.
> 1. Grant your account the **Curator** role, _or_ ask the Zenodo support
>    team to transfer ownership.
> 1. Re-run `fix` once access is granted.

```bash
# Upload every missing release
export ZENODO_TOKEN=your-token-here
zenodo-release-drift fix owner/repo

# Upload a single specific release
zenodo-release-drift fix owner/repo --version 1.2.3

# Upload all missing releases from 1.0.0 onwards
zenodo-release-drift fix owner/repo --from 1.0.0

# Upload all missing releases up to and including 1.4.0
zenodo-release-drift fix owner/repo --to 1.4.0

# Upload missing releases within a range
zenodo-release-drift fix owner/repo --from 1.0.0 --to 1.4.0

# Test against the Zenodo sandbox before touching production
export ZENODO_TOKEN=your-sandbox-token-here
zenodo-release-drift fix owner/repo --sandbox
```

### `version`

```
zenodo-release-drift version
```

## Check codes

| Code   | Description                                                   |
| ------ | ------------------------------------------------------------- |
| ZRD001 | A GitHub release exists with no matching Zenodo archive       |
| ZRD002 | The latest Zenodo version is behind the latest GitHub release |

## Authentication

### GitHub (`GITHUB_TOKEN`)

By default the tool makes unauthenticated GitHub API calls (60 requests/hour limit).
Set `GITHUB_TOKEN` to raise this to 5,000 requests/hour:

```bash
export GITHUB_TOKEN=ghp_...
zenodo-release-drift check my-org
```

### Zenodo (`ZENODO_TOKEN`)

The `fix` command requires a Zenodo personal access token with the
**`deposit:write`** scope.

1. Log in to <https://zenodo.org> (or <https://sandbox.zenodo.org> for testing).
1. Go to **Account → Applications → Personal access tokens**.
1. Create a token with the `deposit:write` scope.
1. Export it before running `fix`:

```bash
export ZENODO_TOKEN=your-token-here
zenodo-release-drift fix owner/repo
```

## Pre-commit hook

Add to `.pre-commit-config.yaml` to gate commits on a single repository.
Set `args` to the repository you want to check:

```yaml
- repo: local
  hooks:
    - id: zenodo-release-drift
      name: Zenodo release drift
      entry: zenodo-release-drift lint
      args: ["owner/repo"]
      language: python
      pass_filenames: false
      always_run: true
```

## Python API

```python
from zenodo_release_drift import lint_repo, lint_repo_explain

# Returns a list of finding dicts
findings = lint_repo("owner", "repo")

# Returns a Markdown string with explanations
report = lint_repo_explain("owner", "repo")
```

## Development

```bash
git clone https://github.com/d33bs/zenodo-release-drift
cd zenodo-release-drift
uv sync --all-groups
uv run poe pipeline   # pre-commit + tests
```
