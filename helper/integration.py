#!/usr/bin/env python3
"""Real Redis health connections/reconnects and TLS identity, disposable only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / '.test-output/health-bin/redis-health'
IMAGE = next(line.split(': ', 1)[1].strip() for line in (ROOT / 'charts/redis/values.yaml').read_text().splitlines()
             if line.startswith('image: '))


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)


def eventually(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, subprocess.CalledProcessError, urllib.error.URLError):
            pass
        time.sleep(.1)
    raise AssertionError('health integration deadline exceeded')


def main():
    if not BINARY.exists():
        raise RuntimeError('Build/install .test-output/health-bin/redis-health first')
    results = []
    with tempfile.TemporaryDirectory(prefix='redis-health-') as temp:
        work = Path(temp)
        run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
            '-keyout', str(work / 'tls.key'), '-out', str(work / 'tls.crt'), '-days', '1',
            '-subj', '/CN=redis.fixture', '-addext', 'subjectAltName=DNS:redis.fixture')
        for secure in [False, True]:
            name = 'redis-health-test-' + uuid.uuid4().hex[:10]
            helper = None
            environment = dict(os.environ, REDISCLI_AUTH='fixture-only-password', REDIS_USERNAME='default',
                               REDIS_TLS_ENABLED='yes' if secure else 'no', REDIS_TLS_SERVER_NAME='redis.fixture',
                               REDIS_TLS_CA_FILE=str(work / 'tls.crt'), REDIS_TLS_CERT_FILE='', REDIS_TLS_KEY_FILE='')
            try:
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    redis_port = reservation.getsockname()[1]
                args = ['docker', 'run', '-d', '--name', name, '--user', f'{os.getuid()}:{os.getgid()}',
                        '-p', f'127.0.0.1:{redis_port}:6379', '--mount', f'type=bind,src={work},dst=/tls,readonly', IMAGE,
                        'redis-server', '--save', '', '--appendonly', 'no', '--requirepass', 'fixture-only-password']
                if secure:
                    args += ['--port', '0', '--tls-port', '6379', '--tls-cert-file', '/tls/tls.crt',
                             '--tls-key-file', '/tls/tls.key', '--tls-ca-cert-file', '/tls/tls.crt', '--tls-auth-clients', 'no']
                run(*args)
                port = json.loads(run('docker', 'inspect', name))[0]['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort']
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    health_port = reservation.getsockname()[1]
                environment.update(REDIS_HEALTH_ADDRESS='127.0.0.1:' + port, REDIS_HEALTH_PORT=str(health_port))
                helper = subprocess.Popen([str(BINARY), '--target', 'redis'], env=environment,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                url = f'http://127.0.0.1:{health_port}/ready'

                def ready():
                    try:
                        with urllib.request.urlopen(url, timeout=1) as response:
                            return response.status == 200
                    except urllib.error.HTTPError as error:
                        return False

                eventually(ready)
                started = time.monotonic()
                initial_stat = Path(f'/proc/{helper.pid}/stat').read_text().rsplit(') ', 1)[1].split()
                initial_ticks = int(initial_stat[11]) + int(initial_stat[12])

                def cli(*command, env=None):
                    return subprocess.run([str(BINARY), 'cli', '-h', '127.0.0.1', '-p', port, *command],
                                          env=env or environment, capture_output=True, text=True, timeout=4)

                def connections():
                    reply = cli('INFO', 'stats')
                    assert reply.returncode == 0
                    return int(next(line.split(':')[1] for line in reply.stdout.splitlines()
                                    if line.startswith('total_connections_received:')))

                before = connections()
                for _ in range(25):
                    assert ready()
                after = connections()
                assert after == before + 1, 'HTTP probes opened Redis connections'
                if secure:
                    wrong = dict(environment, REDIS_TLS_SERVER_NAME='wrong.fixture')
                    assert cli('PING', env=wrong).returncode != 0, 'wrong certificate hostname accepted'
                run('docker', 'stop', '--time', '1', name)
                eventually(lambda: not ready())
                run('docker', 'start', name)
                eventually(ready)
                final_stat = Path(f'/proc/{helper.pid}/stat').read_text().rsplit(') ', 1)[1].split()
                ticks = int(final_stat[11]) + int(final_stat[12]) - initial_ticks
                cpu = ticks / os.sysconf('SC_CLK_TCK') / (time.monotonic() - started) * 1000
                memory = int(next(line.split()[1] for line in Path(f'/proc/{helper.pid}/status').read_text().splitlines()
                                  if line.startswith('VmHWM:')))
                results.append({'tls': secure, 'persistentConnection': True, 'reconnected': True,
                                'measuredCpuMillicores': round(cpu, 2), 'peakRssKiB': memory})
            finally:
                if helper:
                    helper.terminate()
                    try:
                        helper.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        helper.kill()
                        helper.wait(timeout=5)
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({'healthIntegration': results}))


if __name__ == '__main__':
    main()
