"""Pytest bootstrap for the agent test suite.

Pin the AWS region so the suite is hermetic regardless of the developer's shell.

Several modules read ``AWS_REGION`` at IMPORT time (e.g. ``task_agent.REGION =
os.environ.get("AWS_REGION", "us-west-2")``) and build their own boto3 clients from it,
while test fixtures create their moto tables/buckets in a fixed region. When the two
disagree — a developer whose shell exports ``AWS_REGION=us-east-1`` running a fixture
that creates ``plan-state`` in us-west-2 — the code under test writes to a region with
no such table (``ResourceNotFoundException``) and the read-back comes up empty
(``KeyError: 'Item'``). That is purely an environment artifact, not a code defect, and
it is how the planning-runtime tests appear to "fail" on a non-us-west-2 shell.

The repo's region is us-west-2 (staging and prod), which is also the CI condition under
which the suite is green. Pin both region variables here, at collection time — pytest
imports conftest.py before any test module, so an import-time ``os.environ.get(
"AWS_REGION")`` in the code under test sees the pinned value.

Credentials and ``AWS_PROFILE`` are deliberately left untouched: the live-AWS tests opt
in through ``AWS_PROFILE`` and the moto-backed tests set their own dummy credentials.
"""

import os

# Force (not setdefault): a developer's shell may export a different region, which is
# exactly the case this guards against.
os.environ["AWS_REGION"] = "us-west-2"
os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
