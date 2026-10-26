# Regression fixtures

`previous/startup.sh` and `previous/shutdown.sh` preserve earlier behavior of
this deployment implementation for the first-upgrade rehearsal. They are
project-owned MIT-licensed scripts, not copied Bitnami shell libraries. The
fixture starts with numeric announcements, then executes the current migration
and replaces members with reused IPs while retaining data and voter identities.

`previous/resources.json` is an offline render of the corresponding earlier
resource contract with synthetic release/namespace/storage settings. It checks
selectors, Services, PVC templates, resource/security settings, and lifecycle
hooks. It contains no cluster inventory or real Secret payload.

The README's upstream comparison is separately scoped to Bitnami chart 25.3.9,
available from its public OCI registry. The optional legacy-image maintenance
mode accepts independently obtained upstream hook files; the default documented
verification uses the official Redis image throughout.
