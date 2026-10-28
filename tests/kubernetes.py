#!/usr/bin/env python3
"""Rehearse chart adoption, dependency recovery and downgrade in disposable kind."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
NODE_IMAGE = 'kindest/node:v1.32.2@sha256:f226345927d7e348497136874b6d207e0b32cc52154ad8323129352923a3142f'
PASSWORD = 'fixture-system-password'


def run(*args, source=None, check=True, timeout=60):
    result = subprocess.run(args, input=source, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr or result.stdout}')
    return result


def eventually(check, timeout=180):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (RuntimeError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
            last = error
        time.sleep(1)
    raise AssertionError(f'Recovery deadline exceeded: {last}')


class Fixture:
    def __init__(self, work, kind):
        cache = ROOT / '.test-output'
        cache.mkdir(exist_ok=True)
        self.work = work or Path(tempfile.mkdtemp(prefix='kubernetes-', dir=cache))
        self.kind = kind
        name_file = self.work / 'cluster-name'
        if work:
            self.name = name_file.read_text().strip()
        else:
            self.name = 'redis-recovery-' + uuid.uuid4().hex[:8]
            name_file.write_text(self.name)
            config = {'kind': 'Cluster', 'apiVersion': 'kind.x-k8s.io/v1alpha4',
                      'nodes': [{'role': 'control-plane'}, {'role': 'worker'}, {'role': 'worker'}]}
            (self.work / 'kind.json').write_text(json.dumps(config))
            run(kind, 'create', 'cluster', '--name', self.name, '--image', NODE_IMAGE,
                '--config', str(self.work / 'kind.json'), '--kubeconfig', str(self.work / 'kubeconfig'), '--wait', '180s', timeout=360)
        assert self.name.startswith('redis-recovery-')
        assert self.name in run(kind, 'get', 'clusters').stdout.splitlines()
        self.context = 'kind-' + self.name
        self.kubeconfig = str(self.work / 'kubeconfig')
        print(f'Disposable fixture: {self.name}; evidence: {self.work}', flush=True)
        self.results = []
        self.primary = None
        self.held_storage = None
        self.dns_replicas = None
        nodes = self.obj('nodes')['items']
        assert len(nodes) == 3 and all(n['metadata']['name'].startswith(self.name + '-') for n in nodes)
        self.kube('taint', 'node', self.name + '-control-plane', 'node-role.kubernetes.io/control-plane-', check=False)
        # Preload the pinned API utility independently of nested registry/DNS.
        # Docker's containerd image store can save a partial multi-arch index;
        # importing only the host platform avoids requiring its other manifests.
        image = run('yq', '-r', '.migration.kubectlImage', str(ROOT / 'charts/redis/values.yaml')).stdout.strip()
        if run('docker', 'image', 'inspect', image, check=False).returncode:
            run('docker', 'pull', image, timeout=180)
        platform = 'linux/' + run('docker', 'image', 'inspect', '--format', '{{.Architecture}}', image).stdout.strip()
        repository = image.split('@')[0].rsplit(':', 1)[0]
        reference = repository + '@' + image.split('@')[1]
        archive = self.work / 'kubectl-image.tar'
        run('docker', 'save', '-o', str(archive), image)
        for node in nodes:
            with archive.open('rb') as source:
                subprocess.run(['docker', 'exec', '-i', node['metadata']['name'], 'ctr', '--namespace=k8s.io',
                                'images', 'import', '--platform', platform, '--base-name', repository, '--digests', '-'], stdin=source,
                               capture_output=True, check=True, timeout=90)
            listing = run('docker', 'exec', node['metadata']['name'], 'ctr', '--namespace=k8s.io', 'images', 'list').stdout.splitlines()
            # Name the original registry manifest, not Docker's outer OCI archive
            # index. Keep imported aliases that containerd's CRI cache may use.
            aliases = [line.split()[0] for line in listing[1:]
                       if len(line.split()) > 2 and line.split()[2] == image.split('@')[1]]
            if aliases and reference not in aliases:
                run('docker', 'exec', node['metadata']['name'], 'ctr', '--namespace=k8s.io', 'images', 'tag', '--force', aliases[0], reference)
            elif not aliases:
                # Classic Docker archives do not retain the registry index.
                run('docker', 'exec', node['metadata']['name'], 'crictl', 'pull', image, timeout=120)

    def kube(self, *args, source=None, check=True, timeout=40):
        return run('kubectl', '--kubeconfig', self.kubeconfig, '--context', self.context, '--request-timeout=25s',
                   *args, source=source, check=check, timeout=timeout)

    def obj(self, *args):
        return json.loads(self.kube('get', *args, '-o', 'json').stdout)

    def apply(self, obj):
        self.kube('apply', '-f', '-', source=json.dumps(obj))

    def helm(self, *args, timeout=360):
        return run('helm', '--kubeconfig', self.kubeconfig, '--kube-context', self.context, *args, timeout=timeout)

    def cli(self, index, *command, sentinel=False, check=True):
        result = self.kube('exec', '-n', 'database', f'cache-node-{index}', '-c', 'redis', '--',
                           'env', 'REDISCLI_AUTH=' + PASSWORD, 'timeout', '5', 'redis-cli', '--raw', '-e',
                           '-p', '26379' if sentinel else '6379', *command, check=check)
        return result.stdout.strip() if check else result

    def members(self):
        return [p for p in self.obj('pods', '-n', 'database')['items'] if re.fullmatch(r'cache-node-[012]', p['metadata']['name'])]

    def scale(self, count):
        self.kube('scale', 'statefulset', 'cache-node', '-n', 'database', '--replicas=' + str(count))

    def stop(self):
        self.scale(0)
        eventually(lambda: not self.members(), timeout=180)

    def healthy(self, indices=(0, 1, 2)):
        pods = {p['metadata']['name']: p for p in self.members()}
        for index in indices:
            statuses = pods[f'cache-node-{index}']['status'].get('containerStatuses', [])
            if len(statuses) != 3 or not all(c['ready'] for c in statuses):
                return False
        infos = {i: dict(line.split(':', 1) for line in self.cli(i, 'INFO', 'replication').splitlines()
                         if ':' in line and not line.startswith('#')) for i in indices}
        primaries = [i for i in indices if infos[i]['role'] == 'master']
        if len(primaries) != 1:
            return False
        primary = primaries[0]
        for index in indices:
            if index != primary and (infos[index].get('master_link_status') != 'up' or
                                     infos[index].get('master_sync_in_progress') != '0' or
                                     infos[index]['master_replid'] != infos[primary]['master_replid']):
                return False
            if not self.cli(index, 'SENTINEL', 'CKQUORUM', 'redis', sentinel=True).startswith('OK'):
                return False
        self.primary = primary
        return True

    def identities(self):
        resources = {}
        for kind in ['pvc', 'service', 'secret']:
            for obj in self.obj(kind, '-n', 'database')['items']:
                name = obj['metadata']['name']
                if name == 'redis' or name.startswith(('cache', 'redis-data-cache')):
                    resources[kind + '/' + name] = {'uid': obj['metadata']['uid'], 'spec': obj.get('spec'),
                        'data_hash': hashlib.sha256(json.dumps(obj.get('data'), sort_keys=True).encode()).hexdigest()}
        return {'resources': resources, 'voters': [self.cli(i, 'SENTINEL', 'MYID', sentinel=True) for i in range(3)]}

    def rollout_complete(self):
        stateful = self.obj('statefulset', 'cache-node', '-n', 'database')
        status = stateful.get('status', {})
        revision = status.get('updateRevision')
        if not revision or status.get('updatedReplicas') != 3 or status.get('currentRevision') != revision:
            return False
        scripts = next(v['configMap']['name'] for v in stateful['spec']['template']['spec']['volumes'] if v['name'] == 'scripts')
        pods = self.members()
        if len(pods) != 3:
            return False
        for pod in pods:
            if pod['metadata'].get('deletionTimestamp') or pod['metadata']['labels'].get('controller-revision-hash') != revision:
                return False
            if next(v['configMap']['name'] for v in pod['spec']['volumes'] if v['name'] == 'scripts') != scripts:
                return False
        return self.healthy()

    def retained(self):
        assert self.identities() == self.baseline
        for index in range(3):
            result = self.cli(index, 'EVAL', "for i=1,25 do if redis.call('GET','fixture:key:'..i)~=tostring(i) then return 0 end end return 1", '0')
            assert result == '1'

    def record(self, name, started, **details):
        self.results.append({'name': name, 'seconds': round(time.monotonic() - started, 3), **details})
        (self.work / 'summary.json').write_text(json.dumps(self.results, indent=2) + '\n')
        print(json.dumps(self.results[-1]), flush=True)

    def setup(self):
        self.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'database'}})
        self.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'redis', 'namespace': 'database'},
                    'stringData': {'redis-password': PASSWORD}})
        previous = self.work / 'previous-chart'
        (previous / 'templates').mkdir(parents=True, exist_ok=True)
        (previous / 'Chart.yaml').write_text('apiVersion: v2\nname: redis\nversion: 0.1.4\nappVersion: 8.8.3\n')
        (previous / 'values.yaml').write_text('bootstrap: false\n')
        objects = json.loads((ROOT / 'tests/fixtures/previous/resources.json').read_text())
        for obj in objects:
            if obj['kind'] == 'StatefulSet':
                obj['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'] = 'standard'
                for container in obj['spec']['template']['spec']['containers']:
                    for env in container.get('env', []):
                        if env['name'] == 'REDIS_BOOTSTRAP':
                            env['value'] = '__FIXTURE_BOOTSTRAP__'
        self.apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'previous-lifecycle', 'namespace': 'database'},
                    'data': {name: (ROOT / 'tests/fixtures/previous' / name).read_text() for name in ['startup.sh', 'shutdown.sh']}})
        rendered = run('helm', 'template', 'cache', str(ROOT / 'charts/redis'), '--namespace', 'database').stdout
        config = json.loads(run('yq', '-o=json', 'select(.kind == "ConfigMap" and .metadata.name == "cache-runtime-config")', '-', source=rendered).stdout)
        objects.append(config)
        for index, obj in enumerate(objects):
            template = json.dumps(obj, indent=2).replace('"__FIXTURE_BOOTSTRAP__"', '{{ ternary "yes" "no" .Values.bootstrap | quote }}')
            (previous / 'templates' / f'{index}.yaml').write_text(template + '\n')
        self.helm('install', 'cache', str(previous), '-n', 'database', '--set', 'bootstrap=true', '--wait', '--timeout', '5m')
        eventually(self.healthy)
        self.helm('upgrade', 'cache', str(previous), '-n', 'database', '--set', 'bootstrap=false', '--wait', '--timeout', '5m')
        eventually(self.healthy)
        self.cli(self.primary, 'EVAL', "for i=1,25 do redis.call('SET','fixture:key:'..i,tostring(i)) end return 25", '0')
        assert self.cli(self.primary, 'WAITAOF', '1', '2', '5000') == '1\n2'
        self.baseline = self.identities()
        (self.work / 'baseline.json').write_text(json.dumps(self.baseline, indent=2) + '\n')
        self.rollback_revision = json.loads(self.helm('history', 'cache', '-n', 'database', '-o', 'json').stdout)[-1]['revision']

    def upgrade(self):
        started = time.monotonic()
        before = self.obj('statefulset', 'cache-node', '-n', 'database')
        self.helm('upgrade', 'cache', str(ROOT / 'charts/redis'), '-n', 'database', '--set', 'replica.persistence.storageClass=standard',
                  '--set', 'metrics.sentinel.enabled=true', '--wait', '--timeout', '5m')
        eventually(self.rollout_complete)
        after = self.obj('statefulset', 'cache-node', '-n', 'database')
        assert before['metadata']['uid'] != after['metadata']['uid'] and after['spec']['podManagementPolicy'] == 'Parallel'
        assert all(p['metadata']['ownerReferences'][0]['uid'] == after['metadata']['uid'] for p in self.members())
        self.retained()
        self.record('healthy OrderedReady adoption and upgrade', started)

    def rollback(self):
        started = time.monotonic()
        eventually(self.healthy)
        previous_primary = self.primary
        run('env', 'KUBECONFIG=' + str(Path(self.kubeconfig).resolve()), '/bin/bash', str(ROOT / 'scripts/prepare-rollback.sh'),
            self.context, 'database', 'cache', timeout=240)
        eventually(self.healthy)
        assert self.primary != previous_primary
        for index in range(3):
            self.kube('exec', '-n', 'database', f'cache-node-{index}', '-c', 'redis', '--', 'test', '!', '-e', '/run/redis-shutdown/finished')
            answer = self.cli(index, 'SENTINEL', 'GET-MASTER-ADDR-BY-NAME', 'redis', sentinel=True)
            assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\n6379', answer)
        before = self.obj('statefulset', 'cache-node', '-n', 'database')['metadata']['uid']
        self.kube('delete', 'statefulset', 'cache-node', '-n', 'database', '--cascade=orphan', '--wait=true')
        assert len(self.members()) == 3
        self.helm('rollback', 'cache', str(self.rollback_revision), '-n', 'database', '--wait', '--timeout', '5m')
        eventually(self.rollout_complete)
        after = self.obj('statefulset', 'cache-node', '-n', 'database')
        assert after['metadata']['uid'] != before and after['spec']['podManagementPolicy'] == 'OrderedReady'
        assert all(p['metadata']['ownerReferences'][0]['uid'] == after['metadata']['uid'] for p in self.members())
        self.retained()
        self.record('numeric-reply compatibility and controller-only Helm rollback', started)
        self.upgrade()

    def failed_handoff(self):
        started = time.monotonic()
        eventually(self.rollout_complete)
        former = self.primary
        before = self.obj('pod', f'cache-node-{former}', '-n', 'database')
        old_container = next(c['containerID'] for c in before['status']['containerStatuses'] if c['name'] == 'redis')
        self.kube('exec', '-n', 'database', f'cache-node-{former}', '-c', 'redis', '--',
                  'rm', '-f', '/run/redis-shutdown/rollback-handoff-result')
        body = (ROOT / 'scripts/rollback-handoff.sh').read_text()
        # Fail the real hook after its pause by naming a nonexistent Sentinel
        # master set only for this exec, leaving the Pod's startup env intact.
        self.kube('exec', '-i', '-n', 'database', f'cache-node-{former}', '-c', 'redis', '--',
                  'env', 'REDIS_SHUTDOWN_MASTER_SET=fixture-missing', '/bin/bash', '-s', source=body, check=False)
        def restarted():
            pod = self.obj('pod', f'cache-node-{former}', '-n', 'database')
            return any(c['name'] == 'redis' and c.get('containerID') and c['containerID'] != old_container
                       and c.get('state', {}).get('running')
                       for c in pod['status'].get('containerStatuses', []))
        eventually(restarted)
        result = self.kube('exec', '-n', 'database', f'cache-node-{former}', '-c', 'redis', '--',
                           'cat', '/run/redis-shutdown/rollback-handoff-result').stdout.strip()
        assert result and result != '0'
        self.kube('exec', '-n', 'database', f'cache-node-{former}', '-c', 'redis', '--',
                  'rm', '-f', '/run/redis-shutdown/rollback-handoff-result')
        eventually(self.healthy)
        assert self.obj('pod', f'cache-node-{former}', '-n', 'database')['metadata']['uid'] == before['metadata']['uid']
        assert self.primary != former
        self.retained()
        self.record('failed preparation hook fences the paused writer by container restart; native recovery retains data', started)

    def discovery_epochs(self):
        started = time.monotonic()
        source = self.kube('exec', '-n', 'database', 'cache-node-0', '-c', 'redis', '--', 'cat', '/shutdown/startup.sh').stdout
        functions = source[source.index('endpoint()'):source.index('\nhash()')]
        checks = r'''
set -euo pipefail
replicas=3 quorum=2 master_set=redis
endpoint() { printf 'fixture-node-%s' "$1"; }
canonical_host() { case "$1" in fixture-node-[012]) printf '%s' "$1";; *) return 1;; esac; }
cli() {
    local host=$2 index=${2##*-}
    if [[ "$*" == *'INFO replication' ]]; then printf 'role:master\n'; return; fi
    printf 'ip\n%s\nport\n6379\nconfig-epoch\n%s\n' "${fixture_hosts[$index]}" "${fixture_epochs[$index]}"
}
fixture_epochs=(10 10 11)
fixture_hosts=(fixture-node-0 fixture-node-0 fixture-node-1)
if discover_live_primary; then echo 'Older quorum selected despite a newer observed election' >&2; exit 1; fi
fixture_epochs=(10 11 11)
fixture_hosts=(fixture-node-0 fixture-node-1 fixture-node-1)
discover_live_primary
[[ "$master_host" == fixture-node-1 ]]
fixture_epochs=(10 10 12)
fixture_hosts=(fixture-node-0 fixture-node-0 unknown-member)
if discover_live_primary; then echo 'Unknown newer candidate permitted an older quorum' >&2; exit 1; fi
echo 'Newest observed epoch fences older quorum replies'
'''
        self.kube('exec', '-i', '-n', 'database', 'cache-node-0', '-c', 'redis', '--', '/bin/bash', '-s', source=functions + '\n' + checks)
        self.record('actual mounted discovery function rejects older-quorum and unknown-newer-candidate races', started)

    def restore_storage(self):
        if self.held_storage:
            node, path = self.held_storage
            run('docker', 'exec', node, '/bin/bash', '-ec', 'test -f "$1"; test -d "$1.fixture-hold"; rm "$1"; mv "$1.fixture-hold" "$1"', 'restore', path)
            self.held_storage = None

    def storage(self):
        self.stop()
        pvc = self.obj('pvc', 'redis-data-cache-node-0', '-n', 'database')
        pv = self.obj('pv', pvc['spec']['volumeName'])
        path = pv['spec']['hostPath']['path']
        node = pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values'][0]
        assert path.startswith('/var/local-path-provisioner/') and node.startswith(self.name + '-')
        run('docker', 'exec', node, '/bin/bash', '-ec', 'test -d "$1"; test ! -e "$1.fixture-hold"; mv "$1" "$1.fixture-hold"; : > "$1"', 'hold', path)
        self.held_storage = (node, path)
        try:
            self.scale(3)
            eventually(lambda: self.healthy((1, 2)))
            blocked = self.obj('pod', 'cache-node-0', '-n', 'database')
            assert not any(c.get('ready') for c in blocked['status'].get('containerStatuses', []))
            assert blocked['metadata']['name'] in [p['metadata']['name'] for p in self.members()]
            events = self.obj('events', '-n', 'database')['items']
            assert any(e.get('reason') == 'FailedMount' and e['involvedObject']['uid'] == blocked['metadata']['uid'] for e in events)
            before_uid = blocked['metadata']['uid']
            started = time.monotonic()
            self.restore_storage()
            eventually(self.healthy, timeout=240)
            assert self.obj('pod', 'cache-node-0', '-n', 'database')['metadata']['uid'] == before_uid
            self.retained()
            self.record('higher ordinals recover while lower PVC mount waits; storage restoration resumes same Pod', started)
        finally:
            self.restore_storage()
            self.scale(3)

    def metrics(self, index):
        pod = self.obj('pods', '-n', 'database', '-l', f'app.kubernetes.io/name=redis-sentinel-metrics,redis-member={index}')['items'][0]
        ip = pod['status']['podIP']
        script = 'exec 3<>/dev/tcp/"$1"/9121; printf "GET /metrics HTTP/1.0\\r\\nHost: fixture\\r\\nConnection: close\\r\\n\\r\\n" >&3; cat <&3'
        text = self.kube('exec', '-n', 'database', 'cache-node-0', '-c', 'redis', '--', '/bin/bash', '-ec', script, 'metrics', ip).stdout
        assert PASSWORD not in text and 'redis_sentinel_config_' not in text
        return text

    def dns(self):
        minority_started = time.monotonic()
        self.stop()
        self.scale(1)
        eventually(lambda: self.cli(0, 'PING') == 'PONG')
        denied = self.cli(0, 'SET', 'fixture:minority', 'unsafe', check=False)
        assert 'READONLY' in denied.stdout + denied.stderr
        eventually(lambda: re.search(r'^redis_sentinel_master_ckquorum_status\{[^\n]+\} 0$', self.metrics(0), re.M))
        self.record('single voter rejects writes and native exporter reports failed CKQUORUM', minority_started)
        self.stop()
        self.dns_replicas = self.obj('deployment', 'coredns', '-n', 'kube-system')['spec']['replicas']
        try:
            self.kube('scale', 'deployment', 'coredns', '-n', 'kube-system', '--replicas=0')
            eventually(lambda: not self.obj('pods', '-n', 'kube-system', '-l', 'k8s-app=kube-dns')['items'])
            self.scale(3)
            eventually(lambda: len(self.members()) == 3 and all(len(p['status'].get('containerStatuses', [])) == 3 and
                all('running' in c.get('state', {}) for c in p['status']['containerStatuses']) for p in self.members()))
            missing = self.kube('exec', '-n', 'database', 'cache-node-0', '-c', 'redis', '--',
                                'timeout', '2', 'getent', 'ahostsv4', 'cache-node-1.cache-headless.database.svc.cluster.local', check=False)
            assert missing.returncode != 0, 'Peer DNS unexpectedly resolved while all DNS Pods were absent'
            before = {p['metadata']['name']: p['metadata']['uid'] for p in self.members()}
            # Sentinel may block on native DNS lookups. Probe-managed container
            # restarts are allowed; do not require commands to respond before
            # restoring the intentionally unavailable dependency.
            time.sleep(30)
            started = time.monotonic()
            self.kube('scale', 'deployment', 'coredns', '-n', 'kube-system', '--replicas=' + str(self.dns_replicas))
            self.dns_replicas = None
            eventually(self.healthy)
            for pod in self.members():
                assert before[pod['metadata']['name']] == pod['metadata']['uid']
            self.retained()
            for index in range(3):
                eventually(lambda: re.search(r'^redis_sentinel_master_ckquorum_status\{[^\n]+\} 1$', self.metrics(index), re.M))
            self.record('whole DNS outage recovers without manual Redis actions or replacement Pods', started,
                        automatic_container_restarts=sum(c['restartCount'] for p in self.members() for c in p['status']['containerStatuses']))
        finally:
            if self.dns_replicas is not None:
                self.kube('scale', 'deployment', 'coredns', '-n', 'kube-system', '--replicas=' + str(self.dns_replicas))
                self.dns_replicas = None
            self.scale(3)

    def close(self, keep):
        self.restore_storage()
        if not keep:
            run(self.kind, 'delete', 'cluster', '--name', self.name, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', default=os.environ.get('KIND_BINARY', 'kind'))
    parser.add_argument('--work', type=Path, help='Reuse an existing disposable redis-recovery-* kind fixture only')
    parser.add_argument('--keep', action='store_true', help='Keep this disposable cluster for failure diagnosis')
    args = parser.parse_args()
    fixture = Fixture(args.work, args.kind)
    try:
        fixture.setup()
        fixture.upgrade()
        fixture.failed_handoff()
        fixture.rollback()
        fixture.discovery_epochs()
        fixture.storage()
        fixture.dns()
        print(f'PASS: {len(fixture.results)} Kubernetes scenarios; evidence: {fixture.work}', flush=True)
    except Exception:
        for resource in ['pods', 'events', 'statefulset']:
            (fixture.work / (resource + '-failure.json')).write_text(fixture.kube('get', resource, '-n', 'database', '-o', 'json', check=False).stdout)
        for index in range(3):
            for container in ['redis', 'sentinel']:
                text = fixture.kube('logs', '-n', 'database', f'cache-node-{index}', '-c', container, '--tail=60', check=False).stdout
                (fixture.work / f'cache-node-{index}-{container}.log').write_text(text)
                print(f'cache-node-{index}/{container}\n{text[-5000:]}', flush=True)
        raise
    finally:
        fixture.close(args.keep)


if __name__ == '__main__':
    main()
