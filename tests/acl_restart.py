#!/usr/bin/env python3
"""Exercise external ACL recovery across real Redis container restarts."""
import argparse
from pathlib import Path
import time

from recovery import Fixture, eventually, run


def runtime_acl_mode(fixture, index):
    result = run('docker', 'exec', fixture.data(index), 'stat', '-c', '%a', '/run/redis-config/redis.acl')
    return int(result.stdout.strip(), 8)


def assert_client_data(fixture):
    for index in range(3):
        result = run('docker', 'exec', '-e', 'REDISCLI_AUTH=fixture-client-password', fixture.data(index),
                     'redis-cli', '--raw', '-e', '--user', 'client',
                     *(['--tls', '--cacert', '/tls/ca.crt', '--sni', 'redis.fixture'] if fixture.tls else []),
                     '-p', '6379', 'GET', 'fixture:key:0')
        assert result.stdout.strip() == '0'


def wait_for_restart(fixture, indices, index):
    try:
        eventually(lambda: fixture.healthy(indices), timeout=35)
    except AssertionError as error:
        logs = run('docker', 'logs', '--tail', '80', fixture.data(index), check=False)
        detail = logs.stdout + logs.stderr
        failure = "cp: cannot create regular file '/run/redis-config/redis.acl': Permission denied"
        if failure in detail:
            raise AssertionError('captured legacy external ACL restart failure: ' + failure + '\n' + detail) from error
        raise AssertionError('Redis did not recover after container restart. Data container logs:\n' + detail) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tls', action='store_true')
    parser.add_argument('--volatile', action='store_true')
    parser.add_argument('--scripts-dir', type=Path,
                        help='alternate lifecycle scripts directory for a deployed-version negative control')
    args = parser.parse_args()

    fixture = Fixture(tls=args.tls, persistence=not args.volatile, external_acl=True)
    if args.scripts_dir:
        fixture.scripts = args.scripts_dir.resolve()
    (fixture.config / 'users.acl').chmod(0o440)
    assert (fixture.config / 'users.acl').stat().st_mode & 0o777 == 0o440

    try:
        started = time.monotonic()
        for index in range(3):
            fixture.start(index, bootstrap=True)
        eventually(fixture.healthy)
        fixture.write_fixture()
        identities = fixture.ids()
        fixture.bootstrap_file.write_text('no')
        for index in range(3):
            assert run('docker', 'exec', fixture.data(index), 'cat', '/bootstrap/enabled').stdout.strip() == 'no'
        assert_client_data(fixture)
        fixture.record('external ACL pool bootstrapped with source mode 0440; bootstrap disabled', started)

        # Reproduce the mode left by the old cp-under-umask behavior without
        # replacing the runtime directory or any retained Redis state.
        started = time.monotonic()
        replica = next(index for index in range(3) if index != fixture.primary)
        for mode in ['0400', '0600']:
            run('docker', 'exec', fixture.data(replica), 'chmod', mode, '/run/redis-config/redis.acl')
            container_id = run('docker', 'inspect', '-f', '{{.Id}}', fixture.data(replica)).stdout
            fixture.restart_data_container(replica)
            wait_for_restart(fixture, (0, 1, 2), replica)
            assert run('docker', 'inspect', '-f', '{{.Id}}', fixture.data(replica)).stdout == container_id
            assert runtime_acl_mode(fixture, replica) == 0o600
            run('docker', 'exec', fixture.data(replica), 'cmp', '/config/users.acl', '/run/redis-config/redis.acl')
            assert fixture.ids() == identities
            fixture.retained()
            assert_client_data(fixture)
        fixture.record('repeated data-container restart replaces legacy 0400 and current 0600 ACLs', started)

        started = time.monotonic()
        former_primary = fixture.primary
        survivors = [index for index in range(3) if index != former_primary]
        run('docker', 'exec', fixture.data(former_primary), 'chmod', '0400', '/run/redis-config/redis.acl')
        run('docker', 'rm', '-f', fixture.sentinel(former_primary), check=False)
        run('docker', 'stop', fixture.data(former_primary))
        eventually(lambda: fixture.healthy(survivors), timeout=50)
        assert fixture.primary != former_primary
        fixture.retained(survivors)

        fixture.restart_data_container(former_primary)
        wait_for_restart(fixture, (0, 1, 2), former_primary)
        assert runtime_acl_mode(fixture, former_primary) == 0o600
        assert fixture.ids() == identities
        fixture.retained()
        assert_client_data(fixture)
        fixture.record('former primary promoted around, then rejoins with retained runtime and Sentinel identity', started)
        print(f'PASS: {len(fixture.results)} ACL restart scenarios; evidence: {fixture.work}', flush=True)
    except Exception:
        for index in range(3):
            for name in [fixture.data(index), fixture.sentinel(index)]:
                logs = run('docker', 'logs', '--tail', '35', name, check=False)
                print(name, logs.stdout, logs.stderr, flush=True)
        raise
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
