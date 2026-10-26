# Changelog

## 0.2.1

Pin the advertised replica port to 6379 while the local TCP listener is disabled
for private persistence loading. An early replication handshake must not cause
the primary or Sentinel to learn port zero. Add a regression that holds the
private loading window open while inspecting the primary and Sentinel inventory.

## 0.2.0

Initial standalone chart, recovery lifecycle, migration and maintenance suites,
and shared chart-release integration. Superseded by 0.2.1 because repeat CI
exposed the transient port-announcement race described above. Published archives
remain immutable.
