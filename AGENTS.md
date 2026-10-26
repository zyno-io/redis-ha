# Repository guidance

This repository owns the Redis chart, runtime lifecycle, upgrade migrations,
regression tests, and release automation. GitOps consumers own environment
values, Secret references, and reconciliation dependencies.

Keep public source and examples free of organization-specific deployment names,
addresses, credentials, incident history, private URLs, and private commit IDs.
Update the lifecycle contract and relevant tests with behavior changes.
Preserve the chart name `redis`, existing release names, selectors, Services,
and PVC identities during compatible upgrades. Test with disposable resources.
Never enable bootstrap or discard data to recover an existing installation.

Every agent-created commit must include:

Co-authored-by: Codex <codex@openai.com>
