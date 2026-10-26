#!/usr/bin/env python3
"""Exercise actual Redis lifecycle scripts in disposable Docker containers only."""
import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr or result.stdout}')
    return result


def eventually(check, timeout=100):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (RuntimeError, ValueError, KeyError) as error:
            last = error
        time.sleep(0.4)
    raise AssertionError(f'Recovery deadline exceeded: {last}')


def fields(source):
    return dict(line.split(':', 1) for line in source.splitlines() if ':' in line)


class Fixture:
    def __init__(self):
        cache = ROOT / '.test-output'
        cache.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='recovery-', dir=cache))
        self.work.chmod(0o755)
        self.prefix = 'redis-recovery-' + uuid.uuid4().hex[:8]
        self.names = []
        self.generation = 0
        self.scripts = ROOT / 'charts/redis/files'
        self.results = []
        self.image = run('yq', '-r', '.image', str(ROOT / 'charts/redis/values.yaml')).stdout.strip()
        for image in [self.image]:
            if run('docker', 'image', 'inspect', image, check=False).returncode:
                run('docker', 'pull', image)
        self.config = self.work / 'config'
        self.config.mkdir()
        self.secrets = self.work / 'secrets'
        self.secrets.mkdir()
        (self.secrets / 'redis-password').write_text('fixture-system-password')
        (self.secrets / 'client').write_text('fixture-client-password')
        rendered = run('helm', 'template', 'redis', str(ROOT / 'charts/redis'), '--namespace', 'redis',
                       '--set', 'migration.retainLegacyConfigMaps=false').stdout
        path = self.work / 'rendered.yaml'
        path.write_text(rendered)
        configuration = json.loads(run('yq', '-o=json', 'select(.kind == "ConfigMap" and .metadata.name == "redis-runtime-config")', str(path)).stdout)['data']
        for name, source in configuration.items():
            (self.config / name).write_text(source)
        (self.config / 'users').write_text('client\ton\t+@all -@admin\t~fixture:*\t&fixture:*\n')
        run('docker', 'network', 'create', self.prefix)
        subnet = run('docker', 'network', 'inspect', '-f', '{{(index .IPAM.Config 0).Subnet}}', self.prefix).stdout.strip()
        self.network = ipaddress.ip_network(subnet)
        run('docker', 'network', 'rm', self.prefix)
        run('docker', 'network', 'create', '--subnet', subnet, self.prefix)
        self.states = []
        self.service_ips = []
        for index in range(3):
            state = self.work / f'state-{index}'
            state.mkdir(mode=0o777)
            state.chmod(0o777)
            for name in ['data', 'runtime', 'shutdown']:
                (state / name).mkdir(mode=0o777)
                (state / name).chmod(0o777)
            self.states.append(state)

    def data(self, index):
        return f'{self.prefix}-data-{index}'

    def sentinel(self, index):
        return f'{self.prefix}-sentinel-{index}'

    def service(self, index):
        return f'{self.prefix}-service-{index}'

    def member(self, index):
        return f'redis-node-{index}.redis-headless.fixture'

    def address(self, name):
        return run('docker', 'inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name).stdout.strip()

    def common(self, index, bootstrap):
        state = self.states[index]
        return ['--user', '1001:1001', '--read-only', '--tmpfs', '/tmp',
                '--mount', f'type=bind,src={self.scripts},dst=/shutdown,readonly',
                '--mount', f'type=bind,src={self.config},dst=/config,readonly',
                '--mount', f'type=bind,src={self.secrets},dst=/secrets,readonly',
                '--mount', f'type=bind,src={state / "data"},dst=/data',
                '--mount', f'type=bind,src={state / "runtime"},dst=/run/redis-config',
                '--mount', f'type=bind,src={state / "shutdown"},dst=/run/redis-shutdown',
                '-e', 'REDIS_PASSWORD_FILE=/secrets/redis-password', '-e', 'REDIS_SHUTDOWN_MASTER_SET=redis',
                '-e', 'REDIS_SHUTDOWN_REPLICAS=3', '-e', 'REDIS_SENTINEL_QUORUM=2',
                '-e', f'POD_NAME=redis-node-{index}', '-e', f'POD_IP={self.current_ip}', '-e', 'REDIS_NODE_PREFIX=redis-node',
                '-e', 'REDIS_HEADLESS=redis-headless.fixture',
                '-e', 'REDIS_BOOTSTRAP=' + ('yes' if bootstrap else 'no')]

    def start(self, index, bootstrap=False, suppress_dns=False, ip_override=None):
        name = self.data(index)
        if name not in self.names:
            self.names.extend([name, self.sentinel(index)])
        ip = ip_override or str(self.network.network_address + 20 + self.generation * 4 + index)
        self.current_ip = ip
        run('docker', 'run', '-d', '--name', name, '--network', self.prefix, '--ip', ip,
            *(['--network-alias', self.member(index)] if not suppress_dns else []),
            *self.common(index, bootstrap), '--entrypoint', '/bin/bash', self.image, '/shutdown/startup.sh', 'redis')
        run('docker', 'run', '-d', '--name', self.sentinel(index), '--network', 'container:' + name,
            *self.common(index, bootstrap), '--entrypoint', '/bin/bash', self.image, '/shutdown/startup.sh', 'sentinel')

    def stop(self, indices):
        names = [name for index in indices for name in [self.sentinel(index), self.data(index)]]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda name: run('docker', 'rm', '-f', name, check=False), names))
        self.generation += 1

    def cli(self, index, *command, sentinel=False, check=True):
        result = run('docker', 'exec', '-e', 'REDISCLI_AUTH=fixture-system-password', self.sentinel(index) if sentinel else self.data(index),
                     'redis-cli', '--raw', '-e', '-p', '26379' if sentinel else '6379', *command, check=check)
        return result.stdout.strip() if check else result

    def healthy(self, indices=(0, 1, 2)):
        infos = {index: fields(self.cli(index, 'INFO', 'replication')) for index in indices}
        primaries = [index for index, info in infos.items() if info['role'] == 'master']
        if len(primaries) != 1:
            return False
        primary = primaries[0]
        for index, info in infos.items():
            if index != primary and (info.get('master_link_status') != 'up' or info.get('master_sync_in_progress') != '0' or
                                     info.get('master_replid') != infos[primary]['master_replid']):
                return False
            if self.cli(index, 'SENTINEL', 'GET-MASTER-ADDR-BY-NAME', 'redis', sentinel=True) not in [self.member(primary) + '\n6379', self.address(self.data(primary)) + '\n6379']:
                return False
            if not self.cli(index, 'SENTINEL', 'CKQUORUM', 'redis', sentinel=True).startswith('OK'):
                return False
        for index in indices:
            if run('docker', 'exec', self.data(index), '/bin/bash', '/shutdown/shutdown.sh', 'ready', check=False).returncode:
                return False
        self.primary = primary
        return True

    def write_fixture(self):
        for index in range(25):
            self.cli(self.primary, 'SET', f'fixture:key:{index}', str(index))
        assert self.cli(self.primary, 'WAITAOF', '1', '2', '5000') == '1\n2'

    def retained(self, indices=(0, 1, 2)):
        for index in indices:
            for key in range(25):
                assert self.cli(index, 'GET', f'fixture:key:{key}') == str(key)

    def ids(self):
        return [self.cli(i, 'SENTINEL', 'MYID', sentinel=True) for i in range(3)]

    def record(self, name, started):
        self.results.append({'scenario': name, 'seconds': round(time.monotonic() - started, 2)})
        print(json.dumps(self.results[-1]), flush=True)
        (self.work / 'summary.json').write_text(json.dumps(self.results, indent=2) + '\n')

    def close(self):
        for name in reversed(self.names):
            run('docker', 'rm', '-f', name, check=False)
        run('docker', 'network', 'rm', self.prefix, check=False)


def migration(fixture):
    started = time.monotonic()
    fixture.scripts = ROOT / 'tests/fixtures/previous'
    for index in range(3):
        fixture.start(index, bootstrap=True)
    eventually(fixture.healthy)
    fixture.write_fixture()
    identities = fixture.ids()
    epochs = [fixture.cli(i, 'SENTINEL', 'MASTER', 'redis', sentinel=True).splitlines() for i in range(3)]
    epochs = [dict(zip(lines[::2], lines[1::2]))['config-epoch'] for lines in epochs]
    run('docker', 'run', '--rm', '--network', fixture.prefix, '--user', '1001:1001', '--read-only',
        '--mount', f'type=bind,src={fixture.secrets},dst=/secrets,readonly',
        '--mount', f'type=bind,src={ROOT / "charts/redis/files/prepare-hostnames.sh"},dst=/prepare.sh,readonly',
        '-e', 'REDIS_PASSWORD_FILE=/secrets/redis-password', '-e', 'REDIS_SHUTDOWN_MASTER_SET=redis',
        '-e', 'REDIS_SHUTDOWN_REPLICAS=3', '-e', 'REDIS_NODE_PREFIX=redis-node', '-e', 'REDIS_HEADLESS=redis-headless.fixture',
        '--entrypoint', '/bin/bash', fixture.image, '/prepare.sh')
    assert fixture.ids() == identities
    for i in range(3):
        lines = fixture.cli(i, 'SENTINEL', 'MASTER', 'redis', sentinel=True).splitlines()
        assert dict(zip(lines[::2], lines[1::2]))['config-epoch'] == epochs[i]
    fixture.record('healthy numeric-to-hostname migration preserves voter IDs and epochs', started)
    for index in [2, 1, 0]:
        started = time.monotonic()
        ip = fixture.address(fixture.data(index))
        run('docker', 'exec', fixture.data(index), '/bin/bash', '/shutdown/shutdown.sh', 'started')
        def hook(sentinel):
            return run('docker', 'exec', fixture.sentinel(index) if sentinel else fixture.data(index),
                       '/bin/bash', '/shutdown/shutdown.sh', 'sentinel' if sentinel else 'redis', check=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(hook, [False, True]))
        assert all(reply.returncode == 0 for reply in replies), [(r.stdout, r.stderr) for r in replies]
        fixture.stop([index])
        fixture.scripts = ROOT / 'charts/redis/files'
        fixture.start(index, ip_override=ip)
        eventually(fixture.healthy)
        fixture.retained()
        assert fixture.ids() == identities
        time.sleep(15)
        fixture.record(f'original chart shutdown and replacement ordinal {index} with reused IP', started)
    expected = fixture.member(fixture.primary) + '\n6379'
    assert all(fixture.cli(i, 'SENTINEL', 'GET-MASTER-ADDR-BY-NAME', 'redis', sentinel=True) == expected for i in range(3))
    print(f'PASS first-upgrade migration: {fixture.work}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--migration-only', action='store_true')
    args = parser.parse_args()
    fixture = Fixture()
    try:
        if args.migration_only:
            migration(fixture)
            return
        started = time.monotonic()
        for index in range(3):
            fixture.start(index, bootstrap=True)
        eventually(fixture.healthy)
        fixture.write_fixture()
        identities = fixture.ids()
        fixture.record('explicit initial bootstrap', started)

        started = time.monotonic()
        original_ips = [fixture.address(fixture.data(i)) for i in range(3)]
        previous = fixture.primary
        fixture.stop(range(3))
        fixture.start(previous)
        eventually(lambda: fixture.cli(previous, 'PING') == 'PONG')
        time.sleep(5)
        denied = fixture.cli(previous, 'SET', 'fixture:minority', 'unsafe', check=False)
        assert denied.returncode != 0 and 'READONLY' in denied.stdout + denied.stderr
        def minority_detected():
            reply = fixture.cli(previous, 'SENTINEL', 'CKQUORUM', 'redis', sentinel=True, check=False)
            return 'NOQUORUM' in reply.stdout + reply.stderr
        eventually(minority_detected)
        for index in range(3):
            if index != previous:
                fixture.start(index)
        eventually(fixture.healthy)
        assert fixture.ids() == identities
        assert all(fixture.address(fixture.data(i)) != original_ips[i] for i in range(3))
        fixture.retained()
        fixture.record('full restart with changed IPs and minority write denial', started)

        started = time.monotonic()
        previous = fixture.primary
        fixture.stop(range(3))
        survivors = [i for i in range(3) if i != previous]
        for index in survivors:
            fixture.start(index)
        eventually(lambda: fixture.healthy(survivors))
        fixture.retained(survivors)
        fixture.start(previous)
        eventually(fixture.healthy)
        fixture.retained()
        fixture.record('former primary absent during cold recovery, then returns', started)

        if not args.core_only:
            started = time.monotonic()
            fixture.stop(range(3))
            for state in fixture.states:
                directory = state / 'data/appendonlydir'
                lines = (directory / 'appendonly.aof.manifest').read_text().splitlines()
                last = [line.split()[1] for line in lines if ' type i' in line][-1]
                with (directory / last).open('ab') as handle:
                    handle.write(b'*3\r\n$3\r\nSET\r\n$20\r\npartial')
            for index in range(3):
                fixture.start(index)
            eventually(fixture.healthy)
            fixture.retained()
            fixture.record('all members recover native truncated crash tails', started)

            started = time.monotonic()
            replica = next(i for i in range(3) if i != fixture.primary)
            fixture.stop([replica])
            manifest = fixture.states[replica] / 'data/appendonlydir'
            increment = next(manifest.glob('*.incr.aof'))
            with increment.open('ab') as handle:
                handle.write(b'CORRUPT FIXTURE DATA\n')
            checksum = hashlib.sha256(increment.read_bytes()).hexdigest()
            fixture.start(replica)
            eventually(fixture.healthy)
            quarantined = fixture.states[replica] / 'data/recovery-quarantine/appendonlydir' / increment.name
            assert hashlib.sha256(quarantined.read_bytes()).hexdigest() == checksum
            assert fixture.ids() == identities
            fixture.retained()
            fixture.record('corrupt AOF quarantined and rebuilt automatically', started)

            started = time.monotonic()
            fixture.stop([replica])
            (fixture.states[replica] / 'data/appendonlydir/appendonly.aof.manifest').unlink()
            fixture.start(replica)
            eventually(fixture.healthy)
            assert (fixture.states[replica] / 'data/recovery-quarantine/appendonlydir').is_dir()
            fixture.retained()
            fixture.record('missing AOF manifest rebuilt without electing an empty member', started)

            started = time.monotonic()
            run('docker', 'kill', '--signal=TERM', fixture.data(replica))
            eventually(lambda: run('docker', 'inspect', '-f', '{{.State.Running}}', fixture.data(replica)).stdout.strip() == 'false')
            assert run('docker', 'inspect', '-f', '{{.State.ExitCode}}', fixture.data(replica)).stdout.strip() == '0'
            fixture.stop([replica])
            fixture.start(replica)
            eventually(fixture.healthy)
            fixture.retained()
            fixture.record('supervisor forwards graceful termination and retains persistence', started)

            started = time.monotonic()
            replica = next(i for i in range(3) if i != fixture.primary)
            fixture.stop([replica])
            shutil.rmtree(fixture.states[replica] / 'data/appendonlydir')
            (fixture.states[replica] / 'data/dump.rdb').unlink(missing_ok=True)
            fixture.start(replica)
            eventually(lambda: fixture.cli(replica, 'PING') == 'PONG')
            assert fields(fixture.cli(replica, 'INFO', 'replication'))['slave_priority'] == '0'
            eventually(fixture.healthy)
            assert fields(fixture.cli(replica, 'INFO', 'replication'))['slave_priority'] == '100'
            fixture.retained()
            fixture.record('empty data member rejoins without bootstrap', started)

            started = time.monotonic()
            for index in range(3):
                previous_identity = fixture.cli(index, 'SENTINEL', 'MYID', sentinel=True)
                fixture.stop([index])
                (fixture.states[index] / 'data/sentinel/sentinel.conf').unlink()
                fixture.start(index)
                eventually(fixture.healthy)
                assert fixture.cli(index, 'SENTINEL', 'MYID', sentinel=True) != previous_identity
                eventually(lambda: all('num-other-sentinels\n2' in fixture.cli(i, 'SENTINEL', 'MASTER', 'redis', sentinel=True) for i in range(3)))
            fixture.retained()
            fixture.record('lost Sentinel state replaces voters without electorate growth', started)

            started = time.monotonic()
            fixture.stop(range(3))
            for index in range(3):
                fixture.start(index, suppress_dns=index == 0)
            eventually(lambda: fixture.cli(0, 'PING', sentinel=True) == 'PONG')
            before = run('docker', 'inspect', '-f', '{{.State.StartedAt}}', fixture.sentinel(0)).stdout
            assert run('docker', 'exec', fixture.sentinel(0), '/bin/bash', '/shutdown/startup.sh', 'sentinel-live').returncode == 0
            # The member really has no resolvable DNS name. Restoring external
            # DNS/network availability must not require a Redis/Sentinel restart.
            missing = run('docker', 'exec', fixture.data(1), 'getent', 'hosts', fixture.member(0), check=False)
            assert missing.returncode != 0
            ip = fixture.address(fixture.data(0))
            run('docker', 'network', 'disconnect', fixture.prefix, fixture.data(0))
            run('docker', 'network', 'connect', '--ip', ip, '--alias', fixture.member(0), fixture.prefix, fixture.data(0))
            eventually(fixture.healthy)
            assert run('docker', 'inspect', '-f', '{{.State.StartedAt}}', fixture.sentinel(0)).stdout == before
            fixture.retained()
            fixture.record('temporary member DNS loss resumes without process restart', started)

        print(f'PASS: {len(fixture.results)} scenarios; evidence: {fixture.work}', flush=True)
    except Exception:
        for name in fixture.names:
            if '-service-' not in name:
                print(name, run('docker', 'logs', '--tail', '35', name, check=False).stdout, flush=True)
        raise
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
