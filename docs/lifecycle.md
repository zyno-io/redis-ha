# Lifecycle contract

The owning implementations are `charts/redis/files/startup.sh`, `shutdown.sh`,
and `prepare-hostnames.sh`. The chart packages those files in a retained,
content-addressed ConfigMap; no external script ConfigMap is required.

## Transport, credentials, and storage modes

TLS is optional and disabled by default. Persistence is optional and enabled by
default. TLS mode disables plaintext TCP listeners and covers Redis clients,
replication, Sentinel monitoring/peer discovery, lifecycle commands, and metrics.
The private authenticated UNIX socket used during data loading remains local
plaintext IPC. Clients verify the configured CA. Lifecycle and health command clients verify
the expected certificate DNS identity. Native Redis replication and Sentinel
peer links in the pinned version validate certificate chains but do not perform
DNS hostname verification; use a dedicated trusted pool CA and retain ACL and
network isolation. SNI alone does not establish hostname verification. Server private keys mount only in Redis
and Sentinel containers, not exporters.

Operator, replication, Sentinel monitoring/peer, and discovery identities may use
separate existing Secrets. An externally provisioned data ACL file remains the
server authority when selected; the chart does not generate application passwords.
Sentinel discovery access is bounded to discovery/monitoring commands. Legacy
configurations keep the existing default-user authentication behavior.

Persistence-enabled mode preserves the existing PVCs, AOF, recovery/quarantine,
and retained voter history. Persistence-disabled mode uses only per-Pod emptyDir,
disables AOF and RDB saves, and never treats missing persistence as retained data.
Empty replacements remain ineligible for promotion until synchronization succeeds.
Complete volatile loss requires deliberate initialization; recovery never creates
an empty writable member automatically. Storage-mode changes on an existing
StatefulSet are rejected rather than implicitly removing PVCs.

The update strategy is configurable, retaining RollingUpdate by default. Consumers
can disable the chart's permissive NetworkPolicy and supply their own policies.

## Startup and authority

Sentinel starts from its persisted monitor, identity, and election epochs. It
refreshes credentials and transport settings, enables hostname resolution and
announcement, and seeds the fixed member inventory for normal INFO/HELLO
discovery. Redis 8.8.3 accepts temporarily unresolved member names. A missing
persisted Sentinel configuration requires a quorum-verified live primary, except
for explicit initialization; replacement voters must not grow the electorate.

Startup reads all reachable Sentinel MASTER replies. A replica upstream is
accepted only when quorum agrees at the newest observed configuration epoch
and that endpoint directly reports primary role. This discovery never promotes
a data node. Without a verified live primary, a retained node starts read-only
against an unreachable loopback endpoint, allowing native Sentinel to elect an
eligible retained replica. Even a returning former primary starts read-only.
No ordinal preference or cached address can create a writable retained node.

The bootstrap gate is a separately mounted ConfigMap, excluded from Pod checksums
and the Pod environment value. Each daemon startup rereads the current file;
disabling the gate does not roll processes and remains effective after a later
container restart. A missing/invalid configured file fails startup closed.
Only deliberately enabled bootstrap on a truly empty ordinal zero may create
the initial primary. Empty returning nodes have a persisted seed marker and
replica priority zero. The actual readiness probe restores the configured
priority only after a working replication link and completed synchronization.
That eligibility fence survives container restarts.

## Persistence and liveness

Redis loads behind an authenticated private UNIX socket with public TCP disabled
and its replication fence already configured. Its advertised replica port is
explicitly 6379 even while the local listener is disabled; an early replication
handshake must never register port zero with the primary or Sentinel.
Native Redis owns supported
truncated AOF-tail recovery. After successful load, the supervisor opens TCP
6379 and forwards termination signals to the server.

Confirmed invalid persistence, including an orphaned AOF directory without its
manifest, waits for a repair source. Repair requires a quorum-verified remote
primary plus a second remote member with matching replication history, completed
sync, and an offset at least as recent as the captured primary offset. Only
then may local AOF/RDB files move into the single bounded quarantine and a
priority-zero node reseed. Sentinel metadata and credential files are preserved.
Other startup errors exit with data intact. Changed local persistence files
cause a waiting supervisor to retry native loading.

Each target container has its own persistent health checker and launcher. The
checker reuses an authenticated connection; Kubernetes HTTP probes read bounded
cached results instead of creating CLI processes. Redis and Sentinel expose
separate endpoints on ports 9122 and 9123. A failed checker affects only its own
container; the launcher forwards termination, stops remaining children, and reaps
them when either target or checker exits.
Startup/liveness accepts a fresh supervisor heartbeat during loading or
dependency waits; otherwise it requires authenticated PING. Successful results
expire after ten seconds. Command failure/disconnection invalidates readiness.
 Readiness always
checks the real listener and replica link/sync state. Discovery calls are bounded.
Waiting for quorum does not repeatedly reload valid data or declare an unready
Redis member ready.

During a global DNS outage, native Sentinel DNS waits can delay local commands
and Kubernetes probes may restart a container. Recovery resumes after DNS
returns using retained voter identities and data, without manual Redis actions.
The narrower member-DNS fixture also verifies continuity without restarting
its observed Sentinel process.

## Planned termination

The Redis hook pauses writes and captures the replication ID and offset. Both
replicas must be linked, synchronized, and at that boundary before requesting
Sentinel failover. It verifies the new primary's actual role and retained
history, demotes the old primary, disconnects normal/PubSub clients, and unpauses
only the confirmed replica. An uncertain primary is never resumed.

The Redis hook has a 70-second bound; Sentinel waits on its completion barrier
for at most 75 seconds. Pod termination grace is 90 seconds and the write pause
lasts 120 seconds. Kubernetes can terminate after a failed hook: preStop cannot
veto deletion. Parallel creation does not change RollingUpdate's descending,
one-at-a-time replacement, 15-second sustained readiness gate, or PDB.

## First upgrade

The pre-upgrade hook runs only when an existing StatefulSet uses OrderedReady.
It verifies one primary, two synchronized replicas, and usable Sentinel quorum.
All voters enable hostname support before announcements change. Each replica
reconnects separately so its replication handshake advertises its new name.
Discovery is refreshed one voter at a time, preserving identity and epochs.
That healthy migration step is separate from ordinary recovery, which performs
no Sentinel reset.

Named namespace-scoped RBAC permits orphaning only the existing StatefulSet
controller. Pods, selectors, Services, PVCs, and credentials remain intact for
Helm adoption under Parallel policy. An optional Cilium policy admits only the
Job's API, Redis, and DNS traffic. Subsequent upgrades and fresh installations
do not render this hook.

## Independent health telemetry

Optional Sentinel exporters run independently of the Redis Pods, one per fixed
member FQDN. They use the existing exporter image and system Secret, exposing
native `SENTINEL CKQUORUM`, master status, and peer metrics. They neither select
a primary nor repair a member. Configuration metrics remain disabled so
Sentinel credentials cannot become metric labels. A failed or absent Sentinel
must leave its exporter available with `redis_up` zero; monitoring must also
detect missing exporter streams. Alert evaluation and notification routing
belong to the consuming monitoring system.

## Controlled downgrade

The earlier startup fixture accepts numeric Sentinel replies only. A downgrade
therefore requires a healthy, synchronized deployment before preparing numeric
transport with `prepare-hostnames.sh numeric`. Every Redis replica reconnects
with its numeric announcement before each Sentinel refreshes discovery in turn.
Changing Sentinel replies alone can retain duplicate hostname/numeric aliases
that the earlier startup rejects. The preparation preserves voter IDs and
epochs and flushes the normalized configuration before controller replacement.
The operator then runs the synchronized shutdown hook on the current primary
and restarts that Redis container, keeping its Pod and Sentinel alive. The
container-local wrapper always terminates Redis after the hook, including its
failure path, before a failed write pause could expire on an uncertain writer.
A bounded result file survives the container restart and is removed after the
operator checks it. This planned handoff replaces Sentinel's in-memory
hostname-based primary address with a numeric replica address; reply-format
changes alone do not replace that object. After synchronization, repeat numeric
preparation to retire the former-primary alias before any old startup runs.
Native election advances epochs normally; neither transport refresh resets them.
`scripts/prepare-rollback.sh` performs these preparation steps using an explicit
context, namespace, and release. It does not delete controllers or Pods.
Retain hostname resolution, voter IDs, election epochs, all data, credentials,
and both generations of lifecycle ConfigMaps. Orphan only the StatefulSet
controller before Helm restores its earlier OrderedReady specification; never
force a policy replacement that deletes the Pods or their PVCs. Verify adopted
ownership, ordered replacement, replication, and retained data afterward.
This restores the earlier implementation's recovery limitations. Prefer a
forward fix when voting authority or a synchronized topology is unavailable.

The disposable Kubernetes regression exercises this downgrade and a subsequent
upgrade. It also holds a lower ordinal before volume mounting while higher
ordinals recover, restores global DNS without manual Redis actions, and verifies that a
newer observed election epoch prevents selection from an older quorum.
