#!/usr/bin/env python3
"""Test Redis shutdown only in disposable local Docker containers; no Kubernetes writes."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
# Legacy images are optional migration fixtures, never chart deployment inputs.
# The official-image regression reads its pinned image from the chart.
LEGACY_REDIS = "registry-1.docker.io/bitnami/redis@sha256:341edafd5622a1e47e4b3ab379913de84ea4a50d534f40aaa903a3825e0e1cf1"
LEGACY_SENTINEL = "registry-1.docker.io/bitnami/redis-sentinel@sha256:ab4951733cbc8cd8d27257a1db4d0449df3cc0a22d58136630c4ad1ac42eea0c"


def run(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def eventually(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError("Local Redis fixture did not become healthy before deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, help="Reproduce installed Bitnami preStop scripts before testing the replacement")
    parser.add_argument("--official-only", action="store_true", help="Start with official Redis, isolating handoff behavior from image migration")
    parser.add_argument("--migration-hooks-dir", type=Path, help="Use the installed hooks on departing legacy pods during adoption")
    parser.add_argument("--strategy", choices=("custom", "native", "sentinel", "signal"), default="custom",
                        help="Compare the supported hook with test-only native FAILOVER, Sentinel-only failover, or plain SIGTERM")
    args = parser.parse_args()
    if args.baseline_dir and args.official_only:
        parser.error("The installed Bitnami hooks require the legacy-image fixture")
    if args.migration_hooks_dir and (args.official_only or args.baseline_dir):
        parser.error("Migration hooks require the replacement legacy-image fixture")
    if args.strategy != "custom" and not args.official_only:
        parser.error("Handoff comparisons require --official-only")
    cache = ROOT / ".test-output"
    cache.mkdir(parents=True, exist_ok=True)
    client = cache / "client"
    if not (client / "node_modules/ioredis").exists():
        run("npm", "install", "--prefix", str(client), "--ignore-scripts", "--no-audit", "--no-fund", "ioredis@6.0.0")
    native = run("yq", "-r", ".image", str(ROOT / "charts/redis/values.yaml")).stdout.strip()
    for image in ((native,) if args.official_only else (LEGACY_REDIS, LEGACY_SENTINEL, native)):
        # These are immutable digest references. Reuse exact cached images;
        # comparing hooks should not depend on registry availability each run.
        if run("docker", "image", "inspect", image, check=False).returncode != 0:
            run("docker", "pull", image)
    modes = (["baseline"] if args.baseline_dir else []) + ["replacement"]
    for mode in modes:
        prefix = "redis-maintenance-" + uuid.uuid4().hex[:8]
        names = []
        work = Path(tempfile.mkdtemp(prefix=prefix, dir=cache))
        os.chmod(work, 0o777)
        results = work / "results"
        results.mkdir(mode=0o777)
        network = prefix
        hooks = []
        master_handoffs = 0
        print(f"{mode}/{args.strategy}: starting three Redis/Sentinel pairs (official-only={args.official_only})", flush=True)
        try:
            run("docker", "network", "create", network)
            # Let Docker select a free subnet, then recreate it explicitly so
            # replacement pods may use distinct, controlled IP addresses.
            subnet = run("docker", "network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", network).stdout.strip()
            run("docker", "network", "rm", network)
            run("docker", "network", "create", "--subnet", subnet, network)
            configuration = work / "config"
            configuration.mkdir()
            values = work / "values.json"
            values.write_text(json.dumps({"migration": {"retainLegacyConfigMaps": False},
                "auth": {"acl": {"users": [{"username": "fixture-client", "enabled": "on", "commands": "+@all -@admin",
                                            "keys": "~lease:*", "channels": "&lease:*"}]}}}))
            output = run("helm", "template", "redis", str(ROOT / "charts/redis"),
                         "--namespace", "redis", "-f", str(values)).stdout
            parsed = subprocess.run(["yq", "eval-all", "-o=json", 'select(.metadata.name == "redis-runtime-config") | .data', "-"],
                                    input=output, text=True, capture_output=True, check=True)
            for key, contents in json.loads(parsed.stdout).items():
                (configuration / key).write_text(contents)
            secrets = work / "secrets"
            secrets.mkdir()
            (secrets / "redis-password").write_text("fixture-system-password")
            (secrets / "fixture-client").write_text("fixture-client-password")
            nodes = []
            for index in range(3):
                name = f"{prefix}-redis-{index}"
                state = work / f"state-{index}"
                state.mkdir(mode=0o777)
                (state / "data").mkdir(mode=0o777)
                (state / "runtime").mkdir(mode=0o777)
                command = (
                    "printf '%s\\n' 'user default on >fixture-system-password ~* &* +@all' "
                    "'user fixture-client on >fixture-client-password ~lease:* &lease:* +@all -@admin' >/tmp/acl; "
                    "config=/run/redis-shutdown/replica.conf; [[ -f $config ]] || config=/dev/null; "
                    "exec redis-server \"$config\" --dir /data --save '' --appendonly yes --aclfile /tmp/acl --masterauth fixture-system-password "
                )
                if args.official_only:
                    command = command.replace("config=/dev/null", "config=/config/redis.conf")
                else:
                    command += ("--loadmodule /opt/bitnami/redis/lib/redis/modules/redisearch.so "
                                "--loadmodule /opt/bitnami/redis/lib/redis/modules/rejson.so")
                comparison_mount = ["--mount", f"type=bind,src={ROOT / 'tests/handoff-candidates.sh'},dst=/candidates.sh,readonly"]
                initial_security = ["--user", "1001:1001", "--read-only", "--tmpfs", "/tmp"] if args.official_only else []
                names.append(name)
                run("docker", "run", "-d", "--name", name, "--hostname", f"redis-node-{index}",
                    "--network", network, "--network-alias", "redis-sentinel", "--network-alias", f"redis-node-{index}.redis-headless.redis.svc.cluster.local",
                    "--mount", f"type=bind,src={ROOT / 'charts/redis/files'},dst=/shutdown,readonly",
                    "--mount", f"type=bind,src={state},dst=/run/redis-shutdown",
                    "--mount", f"type=bind,src={state / 'data'},dst=/data",
                    "--mount", f"type=bind,src={configuration},dst=/config,readonly",
                    *comparison_mount, *initial_security,
                    "-e", "REDIS_PASSWORD=fixture-system-password", "-e", "REDIS_PORT=6379",
                    "-e", "REDIS_SENTINEL_PORT=26379", "-e", "REDIS_TLS_ENABLED=no",
                    "-e", "REDIS_SENTINEL_TLS_ENABLED=no", "-e", "REDIS_PASSWORD_FILE=/missing",
                    "-e", "REDIS_SHUTDOWN_MASTER_SET=redis", "-e", "REDIS_SHUTDOWN_REPLICAS=3",
                    "--entrypoint", "/bin/bash", native if args.official_only else LEGACY_REDIS, "-ec", command)
                ip = run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name).stdout.strip()
                nodes.append((name, ip, state))
            def cli(index, *command, sentinel=False, json_output=False):
                return run("docker", "exec", "-e", "REDISCLI_AUTH=fixture-system-password", nodes[index][0],
                           "redis-cli", "--json" if json_output else "--raw", "-e", "-p", "26379" if sentinel else "6379", *command).stdout.strip()

            eventually(lambda: all(cli(i, "PING") == "PONG" for i in range(3)))
            for index in (1, 2):
                cli(index, "REPLICAOF", nodes[0][1], "6379")
            for index, (name, ip, state) in enumerate(nodes):
                sentinel = f"{prefix}-sentinel-{index}"
                names.append(sentinel)
                config = f"""port 26379
bind 0.0.0.0
protected-mode no
user default on >fixture-system-password ~* &* +@all
user fixture-client on >fixture-client-password ~* &* +@all
sentinel monitor redis {nodes[0][1]} 6379 2
sentinel auth-pass redis fixture-system-password
sentinel sentinel-pass fixture-system-password
sentinel down-after-milliseconds redis 5000
sentinel failover-timeout redis 10000
sentinel parallel-syncs redis 1
sentinel announce-ip {ip}
"""
                if not args.official_only:
                    # Match the inspected installed chart instead of giving
                    # every legacy restart an artificial random voter ID.
                    legacy_id = hashlib.sha1(f"redis-node-{index}\n".encode()).hexdigest()
                    config += f"sentinel myid {legacy_id}\n"
                conf = work / f"sentinel-{index}.conf"
                conf.write_text(config)
                sentinel_start = ["--entrypoint", "/bin/bash", LEGACY_SENTINEL, "-ec",
                    "if [[ ! -f /tmp/sentinel.conf ]]; then cp /fixture.conf /tmp/sentinel.conf; fi; exec redis-server /tmp/sentinel.conf --sentinel"]
                sentinel_mounts = []
                if args.official_only:
                    # Seed the fixture's known initial primary once, then use
                    # the actual persistent startup from its first Sentinel.
                    # Resetting myid on replacement distorts election timing.
                    directory = state / "data/sentinel"
                    directory.mkdir(mode=0o777)
                    saved = directory / "sentinel.conf"
                    saved.write_text(config)
                    saved.chmod(0o666)
                    sentinel_mounts = [
                        "--mount", f"type=bind,src={state / 'data'},dst=/data",
                        "--mount", f"type=bind,src={state / 'runtime'},dst=/run/redis-config",
                        "--mount", f"type=bind,src={configuration},dst=/config,readonly",
                        "--mount", f"type=bind,src={secrets},dst=/secrets,readonly",
                        "-e", "REDIS_PASSWORD_FILE=/secrets/redis-password",
                        "-e", "REDIS_SHUTDOWN_MASTER_SET=redis", "-e", "REDIS_SENTINEL_QUORUM=2",
                        "-e", "REDIS_SHUTDOWN_REPLICAS=3", "-e", f"POD_NAME=redis-node-{index}",
                        "-e", f"POD_IP={ip}", "-e", "REDIS_NODE_PREFIX=redis-node",
                        "-e", "REDIS_HEADLESS=redis-headless.redis.svc.cluster.local", "-e", "REDIS_BOOTSTRAP=no"]
                    sentinel_start = ["--entrypoint", "/bin/bash", native, "/shutdown/startup.sh", "sentinel"]
                run("docker", "run", "-d", "--name", sentinel, "--network", "container:" + name,
                    "--mount", f"type=bind,src={conf},dst=/fixture.conf,readonly",
                    "--mount", f"type=bind,src={ROOT / 'charts/redis/files'},dst=/shutdown,readonly",
                    "--mount", f"type=bind,src={state},dst=/run/redis-shutdown",
                    *initial_security,
                    "-e", "REDIS_PASSWORD=fixture-system-password", "-e", "REDIS_PASSWORD_FILE=/missing",
                    "-e", f"HOSTNAME=redis-node-{index}",
                    "-e", "REDIS_SENTINEL_PORT=26379", "-e", "REDIS_SENTINEL_TLS_ENABLED=no",
                    *sentinel_mounts, *sentinel_start)
            def healthy():
                return all("OK" in cli(i, "SENTINEL", "CKQUORUM", "redis", sentinel=True) for i in range(3)) and all(
                    "master_link_status:up" in cli(i, "INFO", "replication") for i in (1, 2))
            eventually(healthy)
            if args.official_only:
                run("docker", "run", "--rm", "--network", network, "--user", "1001:1001", "--read-only",
                    "--mount", f"type=bind,src={secrets},dst=/secrets,readonly",
                    "--mount", f"type=bind,src={ROOT / 'charts/redis/files/prepare-hostnames.sh'},dst=/prepare.sh,readonly",
                    "-e", "REDIS_PASSWORD_FILE=/secrets/redis-password", "-e", "REDIS_SHUTDOWN_MASTER_SET=redis",
                    "-e", "REDIS_SHUTDOWN_REPLICAS=3", "-e", "REDIS_NODE_PREFIX=redis-node",
                    "-e", "REDIS_HEADLESS=redis-headless.redis.svc.cluster.local", "--entrypoint", "/bin/bash", native, "/prepare.sh")
            sentinel_ids = [cli(i, "SENTINEL", "MYID", sentinel=True) for i in range(3)]
            # Sentinel discovers replicas via INFO every ten seconds.
            eventually(lambda: "num-slaves\n2" in cli(0, "SENTINEL", "MASTER", "redis", sentinel=True))
            cli(0, "SET", "lease:durable", "preserved-across-image-migration")
            cli(0, "JSON.SET", "lease:json", "$", '{"hello":"retained"}')
            cli(0, "FT.CREATE", "lease:index", "ON", "JSON", "PREFIX", "1", "lease:json", "SCHEMA", "$.hello", "AS", "hello", "TEXT")
            eventually(lambda: all(cli(i, "GET", "lease:durable") == "preserved-across-image-migration" for i in range(3)))
            # Exercise the old binary's persisted RDB preamble, including its
            # module data, instead of testing only command replay from an AOF.
            for index in range(3):
                cli(index, "BGREWRITEAOF")
            eventually(lambda: all("aof_rewrite_in_progress:0" in cli(i, "INFO", "persistence") and
                                   "aof_last_bgrewrite_status:ok" in cli(i, "INFO", "persistence") for i in range(3)))
            if mode == "replacement" and args.strategy == "custom":
                # Authentication errors must fail the hook and release the
                # Sentinel barrier rather than silently claiming a handoff.
                denied = run("docker", "exec", "-e", "REDIS_PASSWORD=wrong-fixture-password", nodes[0][0],
                             "/bin/bash", "/shutdown/shutdown.sh", "redis", check=False)
                assert denied.returncode != 0
                assert (nodes[0][2] / "finished").exists()
                run("docker", "exec", nodes[0][0], "/bin/bash", "/shutdown/shutdown.sh", "started")
                # PING succeeds on a disconnected replica; rollout readiness
                # must still fail, and the primary must reject this preflight.
                cli(2, "REPLICAOF", "127.0.0.1", "1")
                assert cli(2, "PING") == "PONG"
                unready = run("docker", "exec", nodes[2][0], "/bin/bash", "/shutdown/shutdown.sh", "ready", check=False)
                assert unready.returncode != 0
                eventually(lambda: "connected_slaves:1" in cli(0, "INFO", "replication"))
                incomplete = run("docker", "exec", nodes[0][0], "/bin/bash", "/shutdown/shutdown.sh", "redis", check=False)
                assert incomplete.returncode != 0
                assert "client-pause" not in cli(0, "CLIENT", "LIST")
                cli(2, "REPLICAOF", nodes[0][1], "6379")
                eventually(healthy)
                print("replacement: rejected invalid credentials and disconnected-replica readiness/preflight", flush=True)
            lease_name = prefix + "-leases"
            names.append(lease_name)
            run("docker", "run", "-d", "--name", lease_name, "--network", network,
                "--mount", f"type=bind,src={client},dst=/client,readonly",
                "--mount", f"type=bind,src={ROOT / 'tests/lease-client.cjs'},dst=/test.cjs,readonly",
                "--mount", f"type=bind,src={results},dst=/results", "node:24-alpine", "node", "/test.cjs")
            eventually(lambda: (results / "ready").exists())
            time.sleep(6)
            order = [0] if mode == "baseline" else [2, 1, 0, 2, 1, 0]
            for step, index in enumerate(order):
                if mode == "replacement" and step == 3:
                    # The second complete roll adopts an actual ACL/config
                    # change. Existing clients keep their unchanged identity.
                    (secrets / "new-fixture-user").write_text("new-fixture-password")
                    with (configuration / "users").open("a") as handle:
                        handle.write("new-fixture-user\ton\t+@all -@admin\t~new:*\t&new:*\n")
                    with (configuration / "redis.conf").open("a") as handle:
                        handle.write("tcp-keepalive 61\n")
                name = nodes[index][0]
                if "role:master" in cli(index, "INFO", "replication"):
                    master_handoffs += 1
                sentinel = f"{prefix}-sentinel-{index}"
                if mode == "baseline":
                    for target, file in ((name, "prestop-redis.sh"), (sentinel, "prestop-sentinel.sh")):
                        run("docker", "cp", str(args.baseline_dir / file), target + ":/tmp/" + file)
                    # Schedule Redis's first post-failover poll after Sentinel
                    # exits. The installed scripts are unmodified; this is a
                    # permitted interleaving of kubelet's concurrent hooks.
                    delay = work / "baseline-delay.sh"
                    delay.write_text('''redis-cli() {
    if [[ "$*" == *"sentinel failover"* ]]; then
        touch /tmp/failover-requested
    elif [[ "$*" == *"sentinel get-master-addr-by-name"* && -f /tmp/failover-requested && ! -f /tmp/poll-delayed ]]; then
        touch /tmp/poll-delayed
        for ((attempt = 0; attempt < 50; attempt++)); do
            [[ -f /tmp/sentinel-stopped ]] && break
            sleep 0.2
        done
    fi
    /opt/bitnami/redis/bin/redis-cli "$@"
}
export -f redis-cli
''')
                    run("docker", "cp", str(delay), name + ":/tmp/baseline-delay.sh")
                    redis_hook = ["docker", "exec", "-e", "BASH_ENV=/tmp/baseline-delay.sh", name, "/bin/bash", "/tmp/prestop-redis.sh"]
                    sentinel_hook = ["docker", "exec", sentinel, "/bin/bash", "/tmp/prestop-sentinel.sh"]
                elif args.migration_hooks_dir and step < 3:
                    for target, file in ((name, "prestop-redis.sh"), (sentinel, "prestop-sentinel.sh")):
                        run("docker", "cp", str(args.migration_hooks_dir / file), target + ":/tmp/" + file)
                    redis_hook = ["docker", "exec", name, "/bin/bash", "/tmp/prestop-redis.sh"]
                    sentinel_hook = ["docker", "exec", sentinel, "/bin/bash", "/tmp/prestop-sentinel.sh"]
                else:
                    run("docker", "exec", name, "/bin/bash", "/shutdown/shutdown.sh", "started")
                    script = "/shutdown/shutdown.sh" if args.strategy == "custom" else "/candidates.sh"
                    strategy = "redis" if args.strategy == "custom" else args.strategy
                    redis_hook = ["docker", "exec", name, "timeout", "70", "/bin/bash", script, strategy]
                    sentinel_hook = ["docker", "exec", sentinel, "/bin/bash", "/shutdown/shutdown.sh", "sentinel"]
                    if args.strategy == "signal":
                        redis_hook = ["docker", "stop", "-t", "90", name]
                        sentinel_hook = ["docker", "stop", "-t", "90", sentinel]
                def stop_sentinel():
                    result = run(*sentinel_hook, check=False)
                    if args.strategy != "signal" or mode == "baseline":
                        run("docker", "stop", "-t", "5", sentinel)
                    if mode == "baseline":
                        run("docker", "exec", name, "touch", "/tmp/sentinel-stopped")
                    return result
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(stop_sentinel)
                    started = time.monotonic()
                    result = run(*redis_hook, check=False)
                    elapsed = time.monotonic() - started
                    companion = future.result()
                (work / f"hook-{step}-{index}.log").write_text(result.stdout + result.stderr)
                hooks.append({"pod": index, "redisExit": result.returncode,
                              "sentinelExit": companion.returncode, "seconds": round(elapsed, 2)})
                print(json.dumps(hooks[-1]), flush=True)
                (work / "hooks.json").write_text(json.dumps(hooks, indent=2))
                if mode == "replacement" and result.returncode != 0:
                    print(result.stdout + result.stderr, flush=True)
                    print("Client state at handoff failure: " + (results / "stats.json").read_text(), flush=True)
                if mode == "replacement" and not (args.migration_hooks_dir and step < 3):
                    assert result.returncode == companion.returncode == 0, result.stdout + result.stderr
                elif mode == "baseline":
                    assert result.returncode != 0, "Expected the installed-hook sidecar race to fail"
                # Restart only these local processes. Rejoin the pair before
                # proceeding to the next ordinal, like OrderedReady. Reproduce
                # the chart startup script's Sentinel-based replica bootstrap.
                run("docker", "stop", "-t", "5", name)
                address = []
                def discover_primary():
                    answer = cli((index + 1) % 3, "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "redis", sentinel=True).splitlines()
                    if len(answer) != 2:
                        return False
                    target = next((i for i, node in enumerate(nodes) if answer[0] in (node[1], f"redis-node-{i}.redis-headless.redis.svc.cluster.local")), None)
                    if target is None or target == index or "role:master" not in cli(target, "INFO", "replication"):
                        return False
                    address[:] = answer
                    return True
                eventually(discover_primary)
                if mode == "baseline":
                    (nodes[index][2] / "replica.conf").write_text(f"replicaof {address[0]} {address[1]}\n")
                    run("docker", "start", name)
                    eventually(lambda: cli(index, "PING") == "PONG")
                    run("docker", "start", sentinel)
                else:
                    # Recreate both processes with the official image and the
                    # ACTUAL chart configuration/startup scripts. Keep the AOF
                    # volume and UID 1001, like the unchanged PVC template.
                    run("docker", "rm", sentinel, name)
                    state = nodes[index][2]
                    common = ["--user", "1001:1001", "--read-only", "--tmpfs", "/tmp",
                        "--mount", f"type=bind,src={ROOT / 'charts/redis/files'},dst=/shutdown,readonly",
                        "--mount", f"type=bind,src={configuration},dst=/config,readonly",
                        "--mount", f"type=bind,src={secrets},dst=/secrets,readonly",
                        "--mount", f"type=bind,src={state},dst=/run/redis-shutdown",
                        "--mount", f"type=bind,src={state / 'data'},dst=/data",
                        "--mount", f"type=bind,src={state / 'runtime'},dst=/run/redis-config",
                        *comparison_mount,
                        "-e", "REDIS_PASSWORD_FILE=/secrets/redis-password", "-e", "REDIS_SHUTDOWN_MASTER_SET=redis",
                        "-e", "REDIS_SHUTDOWN_REPLICAS=3", "-e", "REDIS_SENTINEL_QUORUM=2",
                        "-e", f"POD_NAME=redis-node-{index}", "-e", "REDIS_NODE_PREFIX=redis-node",
                        "-e", "REDIS_HEADLESS=redis-headless.redis.svc.cluster.local", "-e", "REDIS_BOOTSTRAP=no"]
                    common += ["-e", "REDIS_MIGRATE_SENTINEL_IDENTITY=yes"]
                    # Allocate the pod network before setting POD_IP; startup
                    # waits for a fixture-only entrypoint to release it.
                    (state / "pod-ip").unlink(missing_ok=True)
                    replacement_ip = nodes[0][1].rsplit(".", 1)[0] + "." + str(100 + step)
                    run("docker", "run", "-d", "--name", name, "--hostname", f"redis-node-{index}",
                        "--network", network, "--ip", replacement_ip, "--network-alias", "redis-sentinel", "--network-alias", f"redis-node-{index}.redis-headless.redis.svc.cluster.local",
                        *common, "--entrypoint", "/bin/bash", native, "-ec",
                        "while [[ ! -f /run/redis-shutdown/pod-ip ]]; do sleep 0.1; done; export POD_IP=$(cat /run/redis-shutdown/pod-ip); exec /bin/bash /shutdown/startup.sh redis")
                    ip = run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name).stdout.strip()
                    assert ip != nodes[index][1], "Each replacement must exercise a changed pod IP"
                    nodes[index] = (name, ip, state)
                    (state / "pod-ip").write_text(ip)
                    run("docker", "run", "-d", "--name", sentinel, "--network", "container:" + name,
                        *common, "-e", f"POD_IP={ip}", "--entrypoint", "/bin/bash", native, "/shutdown/startup.sh", "sentinel")
                    eventually(lambda: cli(index, "PING") == "PONG", timeout=90)
                    assert cli(index, "GET", "lease:durable") == "preserved-across-image-migration"
                    assert "retained" in cli(index, "JSON.GET", "lease:json")
                    assert "lease:json" in cli(index, "FT.SEARCH", "lease:index", "retained")
                    scoped = run("docker", "exec", "-e", "REDISCLI_AUTH=fixture-client-password", name,
                                 "redis-cli", "--raw", "-e", "--user", "fixture-client", "GET", "outside:scope", check=False)
                    assert scoped.returncode != 0 and "NOPERM" in scoped.stdout + scoped.stderr, scoped.stdout + scoped.stderr
                eventually(lambda: run("docker", "exec", name, "/bin/bash", "/shutdown/shutdown.sh", "ready", check=False).returncode == 0)
                if mode == "replacement":
                    eventually(lambda: all("OK" in cli(i, "SENTINEL", "CKQUORUM", "redis", sentinel=True) for i in range(3)))
                    assert cli(index, "SENTINEL", "MYID", sentinel=True) == sentinel_ids[index], "Sentinel must retain its identity across replacement/adoption"
                time.sleep(15)
                if mode == "replacement":
                    masters = [i for i in range(3) if "role:master" in cli(i, "INFO", "replication")]
                    assert len(masters) == 1, "The returning pair must leave exactly one primary"
                    expected = f"redis-node-{masters[0]}.redis-headless.redis.svc.cluster.local\n6379"
                    eventually(lambda: all(cli(i, "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "redis", sentinel=True) == expected for i in range(3)))
            if mode == "replacement":
                for index in range(3):
                    assert cli(index, "CONFIG", "GET", "tcp-keepalive") == "tcp-keepalive\n61"
                    permitted = run("docker", "exec", "-e", "REDISCLI_AUTH=new-fixture-password", nodes[index][0],
                                    "redis-cli", "--raw", "-e", "--user", "new-fixture-user", "GET", "new:unused")
                    assert permitted.returncode == 0
                exporter = run("yq", "-r", ".metrics.image", str(ROOT / "charts/redis/values.yaml")).stdout.strip()
                metrics = prefix + "-metrics"
                names.append(metrics)
                run("docker", "run", "-d", "--name", metrics, "--network", "container:" + nodes[0][0],
                    "--user", "1001:1001", "--read-only", "-e", "REDIS_PASSWORD=fixture-system-password",
                    exporter, "--redis.addr=redis://127.0.0.1:6379")
                probe = "(async () => { const response = await fetch('http://redis-node-0.redis-headless.redis.svc.cluster.local:9121/metrics'); const body = await response.text(); if (!body.includes('redis_up 1')) process.exit(1); })()"
                eventually(lambda: run("docker", "exec", lease_name, "node", "-e", probe, check=False).returncode == 0)
                # No Sentinel quorum and bootstrap disabled: an empty node zero
                # must not create a replacement writable primary. Bound the
                # fixture at 3s rather than waiting for the startup retry limit.
                isolated = prefix + "-isolated"
                names.append(isolated)
                empty = work / "isolated-data"
                empty.mkdir(mode=0o777)
                blocked = run("docker", "run", "--name", isolated, "--network", "none", "--user", "1001:1001",
                    "--mount", f"type=bind,src={ROOT / 'charts/redis/files'},dst=/shutdown,readonly",
                    "--mount", f"type=bind,src={configuration},dst=/config,readonly",
                    "--mount", f"type=bind,src={secrets},dst=/secrets,readonly",
                    "--mount", f"type=bind,src={empty},dst=/data", "--tmpfs", "/run/redis-config:uid=1001,gid=1001",
                    "-e", "REDIS_PASSWORD_FILE=/secrets/redis-password", "-e", "REDIS_SHUTDOWN_MASTER_SET=redis",
                    "-e", "REDIS_SHUTDOWN_REPLICAS=3", "-e", "REDIS_SENTINEL_QUORUM=2", "-e", "POD_NAME=redis-node-0",
                    "-e", "POD_IP=127.0.0.1", "-e", "REDIS_NODE_PREFIX=redis-node", "-e", "REDIS_HEADLESS=unavailable.invalid",
                    "-e", "REDIS_BOOTSTRAP=no", "--entrypoint", "timeout", native, "3", "/bin/bash", "/shutdown/startup.sh", "redis", check=False)
                assert blocked.returncode == 124 and not (empty / ".initialized").exists()
            (results / "stop").touch()
            eventually(lambda: run("docker", "inspect", "-f", "{{.State.Running}}", lease_name).stdout.strip() == "false")
            stats = json.loads((results / "stats.json").read_text())
            print(mode + ": " + json.dumps(stats), flush=True)
            if mode == "replacement":
                def acknowledged_writes_retained():
                    audit = {}
                    for index in range(3):
                        missing = []
                        for start in range(0, stats["acknowledgedWrites"], 200):
                            sequence = range(start, min(start + 200, stats["acknowledgedWrites"]))
                            answer = json.loads(cli(index, "MGET", *(f"lease:ack:{item}" for item in sequence), json_output=True))
                            assert len(answer) == len(sequence)
                            missing.extend(item for item, value in zip(sequence, answer) if value != str(item))
                        audit[str(index)] = {"missingCount": len(missing), "firstMissing": missing[:20]}
                    (work / "acknowledged-write-audit.json").write_text(json.dumps(audit, indent=2))
                    return not any(node["missingCount"] for node in audit.values())
                eventually(acknowledged_writes_retained)
                print(f"Retained all {stats['acknowledgedWrites']} acknowledged writes on all three nodes", flush=True)
            (work / "summary.json").write_text(json.dumps({"mode": mode, "strategy": args.strategy,
                "status": "passed", "officialOnly": args.official_only, "masterHandoffs": master_handoffs, "hooks": hooks, "client": stats}, indent=2))
            if mode == "replacement":
                assert stats["lost"] == 0 and stats["renewals"] > 20, stats
                assert master_handoffs >= 2, "Exercise multiple master deletions across two complete rolls"
            # The baseline hook must fail. Lease loss depends on whether this
            # client's switch-master notification beats its queued renewal;
            # record it without forcing a second artificial client failure.
        except Exception as error:
            stats_file = results / "stats.json"
            stats = json.loads(stats_file.read_text()) if stats_file.exists() else None
            (work / "summary.json").write_text(json.dumps({"mode": mode, "strategy": args.strategy,
                "status": "failed", "error": str(error), "officialOnly": args.official_only,
                "masterHandoffs": master_handoffs, "hooks": hooks, "client": stats}, indent=2))
            print(f"{mode}/{args.strategy}: failed: {error}; client={json.dumps(stats)}", flush=True)
            raise
        finally:
            for name in reversed(names):
                log = run("docker", "logs", name, check=False)
                (work / (name + ".log")).write_text(log.stdout + log.stderr)
                run("docker", "rm", "-f", name, check=False)
            run("docker", "network", "rm", network, check=False)
            print(f"Local fixture logs: {work}", flush=True)


if __name__ == "__main__":
    main()
