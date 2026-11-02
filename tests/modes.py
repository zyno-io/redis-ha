#!/usr/bin/env python3
"""Exercise TLS and volatile lifecycle modes with real disposable Redis/Sentinel."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import time

from recovery import Fixture, eventually, run


def replace_empty(fixture, index):
    fixture.stop([index])
    directory = fixture.states[index] / 'data'
    shutil.rmtree(directory)
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    fixture.start(index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tls', action='store_true')
    parser.add_argument('--volatile', action='store_true')
    parser.add_argument('--external-acl', action='store_true')
    parser.add_argument('--only-exporter', action='store_true', help='Stop after the dedicated Sentinel exporter check')
    parser.add_argument('--consumer-hook', type=Path, help='Executable consumer qualification hook; receives a private fixture JSON path')
    parser.add_argument('--consumer-acl', type=Path, help='Synthetic external data ACL additions installed before bootstrap and every replacement')
    args = parser.parse_args()
    if args.consumer_acl and not args.external_acl:
        parser.error('--consumer-acl requires --external-acl')
    if args.only_exporter and not args.external_acl:
        parser.error('--only-exporter requires --external-acl')
    fixture = Fixture(tls=args.tls, persistence=not args.volatile, external_acl=args.external_acl, consumer_acl=args.consumer_acl)

    def consumer(phase):
        if args.consumer_hook is None:
            return
        started = time.monotonic()
        contract = fixture.work / 'consumer-fixture.json'
        contract.touch(mode=0o600)
        contract.write_text(json.dumps({'schema': 1, 'phase': phase, 'dockerNetwork': fixture.prefix,
            'sentinels': [fixture.member(i) + ':26379' for i in range(3)], 'masterSet': 'redis',
            'primary': {'index': fixture.primary, 'address': fixture.member(fixture.primary) + ':6379'},
            'members': [{'index': i, 'address': fixture.member(i) + ':6379', 'container': fixture.data(i)} for i in range(3)],
            'dataAclFile': str(fixture.config / 'users.acl') if fixture.external_acl else None,
            'tls': {'enabled': fixture.tls, 'serverName': 'redis.fixture',
                    'caFile': str(fixture.tls_dir / 'ca.crt') if fixture.tls else None},
            'operator': {'username': fixture.operator, 'passwordFile': str(fixture.secrets / 'redis-password')},
            'discovery': {'username': 'discovery' if fixture.external_acl else 'default',
                          'passwordFile': str(fixture.secrets / ('discovery' if fixture.external_acl else 'redis-password'))}}))
        run(str(args.consumer_hook.resolve()), str(contract))
        fixture.record('external consumer qualification hook ' + phase, started)

    try:
        started = time.monotonic()
        for index in range(3):
            fixture.start(index, bootstrap=True)
        eventually(fixture.healthy)
        fixture.write_fixture()
        fixture.retained()
        identities = [run('docker', 'inspect', '-f', '{{.Id}}', fixture.data(i)).stdout for i in range(3)]
        fixture.bootstrap_file.write_text('no')
        for i in range(3):
            assert run('docker', 'exec', fixture.data(i), 'cat', '/bootstrap/enabled').stdout.strip() == 'no'
            assert run('docker', 'inspect', '-f', '{{.Id}}', fixture.data(i)).stdout == identities[i]
        fixture.record('bootstrap, authenticated transport and replicated data', started)
        consumer('initial')
        for index in range(3):
            assert fixture.cli(index, 'CONFIG', 'GET', 'appendonly') == 'appendonly\n' + ('no' if args.volatile else 'yes')
            assert fixture.cli(index, 'CONFIG', 'GET', 'save') == 'save'
            if args.tls:
                raw = run('docker', 'exec', fixture.data(index), 'redis-cli', '-h', '127.0.0.1', 'PING', check=False)
                assert raw.returncode != 0, 'TLS listener must not accept plaintext'
                wrong_auth = run('docker', 'exec', '-e', 'REDISCLI_AUTH=wrong', fixture.data(index),
                                 'redis-cli', '--raw', '-e', '--tls', '--cacert', '/tls/ca.crt', '--user', fixture.operator, 'PING', check=False)
                assert wrong_auth.returncode != 0
        if args.external_acl:
            metrics = ['docker', 'exec', '-e', 'REDISCLI_AUTH=fixture-sentinel-metrics-password', fixture.sentinel(0),
                       'redis-cli', '--raw', '-e', '--user', 'sentinel-metrics',
                       *(['--tls', '--cacert', '/tls/ca.crt'] if args.tls else []), '-p', '26379']
            assert 'redis' in run(*metrics, 'SENTINEL', 'MASTERS').stdout
            denied = run(*metrics, 'SENTINEL', 'FAILOVER', 'redis', check=False)
            assert denied.returncode != 0 and 'NOPERM' in denied.stdout + denied.stderr
            # Verify the actual pinned exporter, including its scrape commands,
            # against the separately generated Sentinel identity.
            exporter = fixture.prefix + '-sentinel-exporter'
            fixture.names.append(exporter)
            exporter_image = run('yq', '-r', '.metrics.image', str(Path(__file__).resolve().parents[1] / 'charts/redis/values.yaml')).stdout.strip()
            run('docker', 'run', '-d', '--name', exporter, '--network', 'container:' + fixture.data(0),
                '--user', '1001:1001', '--group-add', str(os.getgid()),
                '-e', 'REDIS_USER=sentinel-metrics', '-e', 'REDIS_PASSWORD=fixture-sentinel-metrics-password',
                *(['--mount', f'type=bind,src={fixture.tls_dir},dst=/redis-ca,readonly'] if args.tls else []),
                exporter_image, '--redis.addr=' + ('rediss' if args.tls else 'redis') + '://127.0.0.1:26379',
                '--include-config-metrics=false',
                *(['--tls-ca-cert-file=/redis-ca/ca.crt', '--tls-server-name=redis.fixture'] if args.tls else []))

            def exporter_ready():
                scrape = run('docker', 'exec', fixture.data(0), '/bin/bash', '-ec',
                             "exec 3<>/dev/tcp/127.0.0.1/9121; printf 'GET /metrics HTTP/1.0\\r\\n\\r\\n' >&3; cat <&3",
                             check=False)
                (fixture.work / 'sentinel-exporter.metrics').write_text(scrape.stdout)
                return (scrape.returncode == 0 and '\nredis_up 1\n' in scrape.stdout and
                        '\nredis_exporter_last_scrape_error{err=""} 0\n' in scrape.stdout and
                        '\nredis_sentinel_master_ckquorum_status{' in scrape.stdout)

            eventually(exporter_ready, timeout=30)
            run('docker', 'rm', '-f', exporter)
            fixture.names.remove(exporter)
            if args.only_exporter:
                print(json.dumps({'result': 'PASS', 'scenario': 'dedicated Sentinel exporter', 'tls': args.tls}))
                return
            result = run('docker', 'exec', fixture.data(0), '/bin/bash', '-ec',
                         'export REDISCLI_AUTH=fixture-client-password; redis-cli --raw -e --user client ' +
                         ('--tls --cacert /tls/ca.crt ' if args.tls else '') + 'GET foreign:key', check=False)
            assert result.returncode != 0 and 'NOPERM' in result.stdout + result.stderr
            result = run('docker', 'exec', '-e', 'REDISCLI_AUTH=fixture-discovery-password', fixture.sentinel(0),
                         'redis-cli', '--raw', '-e', '--user', 'discovery',
                         *(['--tls', '--cacert', '/tls/ca.crt'] if args.tls else []),
                         '-p', '26379', 'SENTINEL', 'FAILOVER', 'redis', check=False)
            assert result.returncode != 0 and 'NOPERM' in result.stdout + result.stderr

        if args.tls:
            # SNI alone is not hostname verification: exercise the actual
            # lifecycle command transport against valid-chain wrong-name/CA.
            for variable, value in [('REDIS_TLS_SERVER_NAME', 'wrong.fixture'),
                                    ('REDIS_TLS_CA_FILE', '/tls/wrong-ca.crt')]:
                result = run('docker', 'exec', '-e', variable + '=' + value, fixture.data(0),
                             '/bin/bash', '-ec', 'source /shutdown/transport.sh; cli -h 127.0.0.1 -p 6379 PING', check=False)
                assert result.returncode != 0, variable + ' must fail closed'

        if args.external_acl:
            def health_status(index, port, path):
                script = f'exec 3<>/dev/tcp/127.0.0.1/{port}; printf "GET {path} HTTP/1.0\\r\\n\\r\\n" >&3; read -r line <&3; printf "%s" "$line"'
                return run('docker', 'exec', fixture.data(index), 'timeout', '3', '/bin/bash', '-ec', script).stdout.split()[1]

            started = time.monotonic()
            paused = [fixture.sentinel(i) for i in [1, 2]]
            try:
                for name in paused:
                    run('docker', 'pause', name)
                eventually(lambda: health_status(0, 9123, '/ready') == '503', timeout=25)
                assert health_status(0, 9123, '/live') == '200'
            finally:
                for name in paused:
                    run('docker', 'unpause', name, check=False)
            eventually(fixture.healthy)
            fixture.record('quorum loss makes Sentinel unready without failing liveness', started)

            started = time.monotonic()
            replicas = [i for i in range(3) if i != fixture.primary]
            try:
                # Replace whole Docker Pod facsimiles: restarting only the data
                # container would strand Sentinel in its old shared netns.
                fixture.stop(replicas)
                eventually(lambda: health_status(fixture.primary, 9122, '/ready') == '503', timeout=15)
                assert health_status(fixture.primary, 9122, '/live') == '200'
                denied = fixture.cli(fixture.primary, 'SET', 'fixture:no-replica', 'unsafe', check=False)
                assert denied.returncode != 0 and 'NOREPLICAS' in denied.stdout + denied.stderr
            finally:
                for index in replicas:
                    fixture.start(index)
            eventually(fixture.healthy)
            fixture.retained()
            fixture.record('primary without replicas is unready and rejects writes while staying live', started)

        started = time.monotonic()
        replica = next(i for i in range(3) if i != fixture.primary)
        previous_ip = fixture.address(fixture.data(replica))
        replace_empty(fixture, replica)
        eventually(fixture.healthy)
        fixture.retained()
        assert fixture.address(fixture.data(replica)) != previous_ip
        for i in range(3):
            reply = fixture.cli(i, 'SENTINEL', 'SENTINELS', 'redis', sentinel=True).splitlines()
            assert reply.count('runid') == 2, 'replacement must leave exactly three current voters'
        fixture.record('empty changed-IP replacement synchronizes without extra voters', started)

        started = time.monotonic()
        previous = fixture.primary
        fixture.stop([previous])
        survivors = [i for i in range(3) if i != previous]
        eventually(lambda: fixture.healthy(survivors))
        fixture.retained(survivors)
        assert fixture.primary != previous
        # Simulate loss of the old primary Pod, including all local authority.
        directory = fixture.states[previous] / 'data'
        shutil.rmtree(directory)
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        fixture.start(previous)
        eventually(fixture.healthy)
        fixture.retained()
        fixture.record('unplanned primary loss and safe empty former-primary rejoin', started)
        consumer('post-promotion')

        started = time.monotonic()
        departing = fixture.primary
        run('docker', 'exec', fixture.data(departing), '/bin/bash', '/shutdown/shutdown.sh', 'started')
        def hook(sentinel):
            return run('docker', 'exec', fixture.sentinel(departing) if sentinel else fixture.data(departing),
                       '/bin/bash', '/shutdown/shutdown.sh', 'sentinel' if sentinel else 'redis', check=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(hook, [False, True]))
        assert all(r.returncode == 0 for r in replies), [(r.stdout, r.stderr) for r in replies]
        replace_empty(fixture, departing)
        eventually(fixture.healthy)
        fixture.retained()
        fixture.record('planned handoff preserves data and returns departing member as replica', started)

        if args.volatile:
            started = time.monotonic()
            fixture.stop(range(3))
            for state in fixture.states:
                shutil.rmtree(state / 'data')
                (state / 'data').mkdir(mode=0o777)
                (state / 'data').chmod(0o777)
            for index in range(3):
                fixture.start(index)
            eventually(lambda: all(fixture.cli(i, 'PING', check=False).returncode == 0 for i in range(3)))
            time.sleep(8)
            for index in range(3):
                result = fixture.cli(index, 'SET', 'fixture:lost', 'unsafe', check=False)
                assert result.returncode != 0 and 'READONLY' in result.stdout + result.stderr
            fixture.record('complete volatile loss does not automatically create a writable primary', started)
        print(json.dumps({'result': 'PASS', 'tls': args.tls, 'persistence': not args.volatile,
                          'externalAcl': args.external_acl, 'scenarios': len(fixture.results), 'evidence': str(fixture.work)}))
    except Exception:
        for name in fixture.names:
            logs = run('docker', 'logs', '--tail', '25', name, check=False)
            print(name, logs.stdout, logs.stderr, flush=True)
        raise
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
