# Persistent health checker

The checker owns health for one Redis or Sentinel container. A single authenticated
RESP2 connection is reused; TLS validates the configured CA and server name.
HTTP requests read cached results and never open Redis connections. Polling uses
two-second command deadlines, five-second intervals, and ten-second maximum
result age. Readiness starts false and is invalidated on connection failures.

The binary also supplies `redis-health cli` for bounded, one-shot lifecycle
network commands. It supports `--raw`, `-e`, `-h`/`--host`, `-p`/`--port`,
`--user`, TLS certificate/CA/SNI options, and `-t` for a whole-call deadline.
Credentials come from `REDISCLI_AUTH` or `REDIS_PASSWORD_FILE`, never password
arguments. Unlike the native CLI's SNI option, this client verifies the server
certificate's DNS identity. Raw replies flatten arrays, and protocol/transport
errors exit nonzero. Private UNIX-socket preload still uses the native CLI.

Diagnostic output uses fixed failure categories and readiness/liveness
transitions. Messages omit server replies, TLS payloads, addresses and
credentials. Transitions are limited to one per ten seconds, with reminders
for an unchanged failure at most once per minute.

`/startup` and `/live` require a responsive command connection or a fresh existing
startup-supervisor heartbeat. `/ready` never accepts a heartbeat. Redis readiness
checks the actual role and replication state; a completed first synchronization
restores the seed marker's configured replica priority before removing the
marker. Sentinel quorum failures affect readiness while responsive PING remains
live. Results and HTTP responses contain no credentials or server payloads.

The init image runs `install /health-bin` to copy the static binary and embedded
launcher. Redis and Sentinel keep their official image and each run:

```text
/bin/bash /health-bin/launcher.sh redis
/bin/bash /health-bin/launcher.sh sentinel
```

The launcher owns the checker and existing startup process. It forwards
termination, stops the remaining child when either exits, and returns the target
exit status. A failed checker cannot restart the other container. Existing
preStop hooks and the shared termination barrier remain active.

Inputs reuse the chart's `REDIS_PASSWORD_FILE`, `REDIS_USERNAME`,
`REDIS_TLS_ENABLED`, `REDIS_TLS_CA_FILE`, `REDIS_TLS_CERT_FILE`,
`REDIS_TLS_KEY_FILE`, `REDIS_TLS_SERVER_NAME`, and `REDIS_SHUTDOWN_MASTER_SET`.
The private preload UNIX socket remains owned by the startup script; the checker
only polls the public local listener and reports unready until it is available.

Optional controls:

- `REDIS_HEALTH_PORT`: 9122 for Redis, 9123 for Sentinel by default.
- `REDIS_HEALTH_ADDRESS`: local target address; defaults to port 6379 or 26379.
- `REDIS_HEALTH_REQUIRE_QUORUM`: `yes` by default; `no` preserves PING-only Sentinel readiness.
- `REDIS_HEALTH_MIN_REPLICAS`: minimum connected replicas for primary readiness, default zero.

Run `go test -race ./...` from this directory. The image build has no runtime Go
or package-manager dependency. Chart publication must pin its immutable image
digest after the helper image passes tests and publication.
The package must allow anonymous pulls before a public chart release references
it; creating a new GHCR package can require an initial visibility change.
