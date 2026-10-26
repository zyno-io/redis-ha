# Redis HA

A self-contained Helm chart for a three-member Redis/Sentinel deployment using
the pinned Docker Official Redis image. It owns startup, recovery, planned
handoff, migration, and the tests for those behaviors. GitOps repositories only
need the published chart version, environment values, and Secret references.

The repository is `redis-ha`; the Helm chart name is **`redis`**. Keeping the
chart name preserves compatible Helm release and storage identities during
adoption. This is a deployment package, not a fork of the Redis server.

## Why the original chart was insufficient

The comparison baseline is **Bitnami Redis chart 25.3.9 in Sentinel mode**.
That chart already supports Sentinel, hostnames, configurable Pod management,
and write-pausing termination hooks. The missing contract is coordinated,
verified lifecycle behavior across Redis, Sentinel, persisted state, and Pod
replacement. Enabling Sentinel or changing a single value does not establish
that contract.

| Concern | Baseline behavior | This chart's contract |
| --- | --- | --- |
| Startup without reachable Sentinel | The startup script can select master mode from the local persisted self-address when Sentinel discovery fails. | A retained member starts read-only. Native Sentinel election authorizes promotion after voting and candidate discovery recover. |
| Concurrent Redis/Sentinel termination | Sentinel's hook can finish after the advertised primary changes, while Redis's hook still needs the local Sentinel process. | A shared completion barrier holds Sentinel until Redis's bounded handoff hook finishes, including its failure path. |
| Handoff completion | The hook polls the primary address returned by Sentinel. | The hook also verifies actual primary role and frozen replication history/offset, demotes the departing primary, disconnects clients, and unpauses the confirmed replica. |
| Complete restart | Values can enable hostnames and Parallel creation; those settings alone do not verify the whole recovery path. | Changed-IP cold restart, an absent former primary, returning members, and minority startup are explicit regression cases. |
| Unusable local persistence | The baseline has no guarded two-copy quarantine-and-reseed lifecycle. | Native Redis first repairs supported crash tails; confirmed invalid local files can be quarantined and rebuilt only after two remote copies are verified. |
| Runtime packaging | Startup depends on Bitnami-specific images, paths, and shell libraries. | Redis and Sentinel use one pinned official image; all lifecycle scripts ship inside the chart. |

These are claims about the evaluated version and configuration, not every
upstream release. The original chart source can be retrieved independently:

```bash
helm pull oci://registry-1.docker.io/bitnamicharts/redis --version 25.3.9 --untar
```

Review its `templates/scripts-configmap.yaml` and Sentinel StatefulSet alongside
this repository's [lifecycle contract](docs/lifecycle.md) and executable tests.
The workload still uses native Redis Sentinel election; the chart does not add
a competing promotion controller.

## Install

Requirements are three schedulable nodes for the default hard anti-affinity,
working cluster DNS, three persistent volumes, and a Secret supplied by the
operator. The chart does not generate or rotate credentials. Each member runs
Redis, Sentinel, and an exporter as non-root processes with read-only root
filesystems.

```bash
helm repo add zyno https://zyno-io.github.io/charts/
helm repo update
kubectl create namespace cache
kubectl create secret generic redis-auth --namespace cache \
  --from-file=redis-password=/path/to/redis-password

# Enable bootstrap only for this deliberate, entirely empty installation.
helm upgrade --install cache zyno/redis --version 0.2.1 --namespace cache \
  --set auth.existingSecret=redis-auth --set bootstrap=true --wait

# Disable it after the three members are healthy, keeping this exact version.
helm upgrade cache zyno/redis --version 0.2.1 --namespace cache \
  --reuse-values --set bootstrap=false --wait
```

Normal upgrades and recovery keep `bootstrap: false`. Never enable it to repair
retained data. The default empty storage-class value uses the cluster default;
set `replica.persistence.storageClass` and `.size` for the target environment.

Clients connect to `<release>.<namespace>.svc.<clusterDomain>:26379` using a
Sentinel-aware driver, master set `redis`, and the configured credentials.
The Service's port 6379 selects all members and is **not a primary-only write
endpoint**. Sentinel replies contain member hostnames; clients must resolve
those names and rediscover the primary after failover.

## Recovery and limits

Existing member FQDNs provide stable identities across changed Pod IPs.
Parallel creation lets all voters and data members return while RollingUpdate
still replaces one Pod at a time. A returning Redis member loads retained data
read-only; Sentinel uses retained voter identities and epochs to elect an
eligible primary. A returning former primary joins that elected primary.

Empty or rebuilt members have priority zero until replication is complete.
Data loading happens behind an authenticated private UNIX socket before TCP
6379 opens. Native Redis handles recoverable AOF crash tails. Confirmed invalid
AOF/RDB files are retained in one bounded quarantine only after a quorum-agreed
remote primary and a synchronized second remote copy exist. Waiting resumes
automatically when the required DNS, authority, storage, or usable copies return.

The chart retains Redis asynchronous replication and `appendfsync everysec`
durability limits. It cannot manufacture lost data, recover permanent loss of
all voting authority, or fence an already-running isolated primary beyond
native Sentinel guarantees. Minority **startup** stays read-only. Redis and
Sentinel transport is authenticated plaintext; network access must be restricted
to trusted clients and members. TLS and cross-site failover are outside this
chart's supported contract.

The fixed topology is three voters with quorum two. Candidate timeout values
and readiness behavior are part of the tested contract; `downAfterMilliseconds`
must be at least 5000. JSON and Search modules are loaded from the pinned Redis
8.8.3 image. Image licenses apply separately from this chart's MIT license.

## Upgrade and adoption

Preserve the existing release name, namespace, member selectors, Services, and
PVC names. Never uninstall an existing Redis release as an upgrade procedure.
Legacy ConfigMap/Secret retention is opt-in through the documented `migration`
values; it copies existing payload and ownership through Helm lookup rather
than creating new credentials. Offline rendering cannot substitute for those
live objects during a real migration.

The first upgrade from OrderedReady checks a healthy synchronized topology,
migrates numeric announcements to member names, reconnects replicas one at a
time, and refreshes Sentinel discovery while preserving voter IDs and epochs.
A temporary, named, namespace-scoped Job then orphans only the StatefulSet
controller. Helm recreates it with Parallel policy and adopts the existing
Pods and PVCs. Ordinary recovery needs no Kubernetes API access. The Job's
pinned kubectl version must be compatible with the target API server.

`migration.ciliumNetworkPolicy` defaults to false. Enable it when Cilium
default-deny policy requires the migration Job's explicit API, Redis, and DNS
egress exception. Other policy engines need equivalent environment-owned
permissions.

Lifecycle ConfigMaps have content-derived names and Helm's keep annotation.
Retain old versions while a Pod or rollback release references them; remove
unreferenced versions deliberately. A downgrade to OrderedReady also requires
controller-only orphan/recreation. Prefer a forward fix preserving Parallel
policy, hostname/election state, credentials, and PVCs. A disposable Kubernetes
adoption/rollback rehearsal and workload-specific client checks remain necessary
before a live rollout; Docker tests do not prove API-server adoption behavior.

## Verify

Install Helm, Mike Farah yq, Python 3, shellcheck, and Docker. Tests create only
disposable Docker resources and ignored local results; fixture credentials are
synthetic. No private repository, cluster context, or real Secret is needed.

```bash
shellcheck charts/redis/files/*.sh scripts/package.sh
python3 tests/render.py
python3 tests/recovery.py
python3 tests/recovery.py --migration-only
python3 tests/maintenance.py --official-only
bash scripts/package.sh v0.2.1
```

The recovery suite checks eleven failure/restoration cases. The upgrade fixture
uses preserved pre-recovery lifecycle behavior and reused Pod IPs. Maintenance
checks two complete rolls with an ordinary Sentinel client, lease renewal,
acknowledged writes, JSON/Search persistence, ACL changes, and exporter health.
The previous-behavior fixtures are frozen regression inputs, not alternate
deployment implementations. See [fixture provenance](tests/fixtures/README.md).

## Release

CI verifies rendering, Docker recovery, first upgrade, and warm maintenance.
A `v<Chart.yaml version>` tag publishes an immutable source release and the
same `.tgz` as a `redis-<version>` release in
[`zyno-io/charts`](https://github.com/zyno-io/charts). That repository's existing
release workflow updates its Helm index automatically.

Repository secret `CHARTS_REPO_TOKEN` must grant Contents read/write access to
`zyno-io/charts`. Source-release writes use the normal `GITHUB_TOKEN`. Runtime
credentials never enter chart releases. Chart version and Redis appVersion are
independent; a chart release must not overwrite the pinned server version.
