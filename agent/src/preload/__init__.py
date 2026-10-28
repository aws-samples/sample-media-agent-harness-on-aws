"""Deploy-time preload: stand up the owner, projects, assets and skills from a package in the Ingest
bucket, before the first user logs in (design docs/design-2026-10-27-admin-preload-and-preanalyzed-ingest.md).

Pure, import-only orchestration split by concern:

- :mod:`preload.ids`       — the stable ``uuid5``-derived Project id (``p`` + 32 hex).
- :mod:`preload.package`   — find the project directories (each holds a ``manifest.json``) under the root.
- :mod:`preload.manifest`  — validate one project's ``manifest.json`` (name / indexing / assets).
- :mod:`preload.globs`     — expand ``assets`` globs + explicit entries against the project's files.
- :mod:`preload.projects`  — seed the four Project rows, ``metaItem``-parity with the proxy.
- :mod:`preload.analysis`  — validate a preanalyzed bundle and build the reusable ``steps[]``.
- :mod:`preload.assets`    — place ``raw`` media in the import inbox / register ``preanalyzed`` assets.
- :mod:`preload.owner`     — resolve ``ownerEmail`` → Cognito ``sub`` (create if absent).
- :mod:`preload.skills`    — install a project's ``skills/SKILL.md`` under the owner (new version on change).
- :mod:`preload.runner`    — tie the above together for one package.

The CloudFormation custom-resource entry point is :mod:`preload.handler`. Nothing here imports boto3 at
module load; clients are passed in, so every unit is testable with fakes / moto.
"""
