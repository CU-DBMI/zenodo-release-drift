# Python API

## Public interface

The top-level package re-exports the two most commonly used functions:

```python
from zenodo_release_drift import lint_repo, lint_repo_explain
```

## Reference

```{eval-rst}
.. autofunction:: zenodo_release_drift.main.lint_repo

.. autofunction:: zenodo_release_drift.main.lint_repo_explain

.. autofunction:: zenodo_release_drift.main.explain_finding

.. autofunction:: zenodo_release_drift.main.check_user
```

## Core classes

```{eval-rst}
.. autoclass:: zenodo_release_drift.main.GitHubCollector
   :members:

.. autoclass:: zenodo_release_drift.main.ZenodoCollector
   :members:

.. autoclass:: zenodo_release_drift.main.VersionMatcher
   :members:

.. autoclass:: zenodo_release_drift.main.DriftEngine
   :members:

.. autoclass:: zenodo_release_drift.main.GitHubUserCollector
   :members:

.. autoclass:: zenodo_release_drift.main.CheckUserResult
   :members:
```
