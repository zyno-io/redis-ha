#!/usr/bin/env python3
"""Offline chart regression: lifecycle packaging, upgrade guards, and ownership."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args, source=None):
    return subprocess.run(args, input=source, text=True, capture_output=True, check=True).stdout


def objects(source):
    result = json.loads(run('yq', 'ea', '-o=json', '. as $item ireduce ([]; . + [$item])', '-', source=source))
    return {(item['kind'], item['metadata']['name']): item for item in result if item}


def main():
    output = ROOT / '.test-output'
    output.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='render-', dir=output))
    chart = work / 'chart'
    shutil.copytree(ROOT / 'charts/redis', chart)
    # Simulate lookup only in a disposable copy. The published chart has no
    # offline bypass for existing-resource or immutable-policy migration checks.
    legacy = chart / 'templates/legacy.yaml'
    legacy.write_text(legacy.read_text().replace('lookup "v1" $kind $.Release.Namespace $name',
                                               'index $.Values.fixtureLegacyObjects $kind $name'))
    migration = chart / 'templates/parallel-migration.yaml'
    migration.write_text(migration.read_text().replace(
        'lookup "apps/v1" "StatefulSet" .Release.Namespace (printf "%s-node" .Release.Name)', '.Values.fixtureStatefulSet'))
    stateful = chart / 'templates/statefulset.yaml'
    stateful.write_text(stateful.read_text().replace(
        'lookup "apps/v1" "StatefulSet" .Release.Namespace (printf "%s-node" .Release.Name)', '.Values.fixtureStatefulSet'))
    baseline = json.loads((ROOT / 'tests/fixtures/previous/resources.json').read_text())
    before = {(item['kind'], item['metadata']['name']): item for item in baseline}
    values = {'replica': {'persistence': {'storageClass': 'fixture-storage'}},
              'fixtureStatefulSet': before[('StatefulSet', 'cache-node')]}
    path = work / 'values.json'

    def render(upgrade=False):
        path.write_text(json.dumps(values))
        return objects(run('helm', 'template', 'cache', str(chart), '--namespace', 'database', '-f', str(path),
                           *(['--is-upgrade'] if upgrade else [])))

    after = render(upgrade=True)
    for key, item in before.items():
        if key[0] != 'StatefulSet':
            assert item == after[key], key
    old = before[('StatefulSet', 'cache-node')]['spec']
    new = after[('StatefulSet', 'cache-node')]['spec']
    for field in ['selector', 'serviceName', 'volumeClaimTemplates', 'updateStrategy', 'minReadySeconds']:
        assert old[field] == new[field], field
    assert old['podManagementPolicy'] == 'OrderedReady' and new['podManagementPolicy'] == 'Parallel'
    # A rolling transport toggle would strand the first replacement on the
    # opposite protocol; fail before either normal resources or hooks run.
    values['tls'] = {'enabled': True, 'existingSecret': 'fixture-tls'}
    path.write_text(json.dumps(values))
    rejected = subprocess.run(['helm', 'template', 'cache', str(chart), '--namespace', 'database', '-f', str(path), '--is-upgrade'],
                              capture_output=True, text=True)
    assert rejected.returncode != 0 and 'Changing TLS mode' in rejected.stderr
    values['tls']['enabled'] = False
    original = values['fixtureStatefulSet']
    values['fixtureStatefulSet'] = copy.deepcopy(original)
    containers = values['fixtureStatefulSet']['spec']['template']['spec']['containers']
    next(c for c in containers if c['name'] == 'redis').setdefault('env', []).append({'name': 'REDIS_TLS_ENABLED', 'value': 'yes'})
    path.write_text(json.dumps(values))
    rejected = subprocess.run(['helm', 'template', 'cache', str(chart), '--namespace', 'database', '-f', str(path), '--is-upgrade'],
                              capture_output=True, text=True)
    assert rejected.returncode != 0 and 'Changing TLS mode' in rejected.stderr
    values['fixtureStatefulSet'] = original
    values.pop('tls')
    for previous, current in zip(old['template']['spec']['containers'], new['template']['spec']['containers']):
        for field in ['name', 'image', 'resources', 'securityContext', 'volumeMounts', 'lifecycle']:
            if field == 'volumeMounts':
                assert previous.get(field, []) == [v for v in current.get(field, []) if v['name'] not in ['bootstrap', 'health-bin']], field
            else:
                assert previous.get(field) == current.get(field), field
    lifecycle_key = next(key for key in after if key[0] == 'ConfigMap' and key[1].startswith('cache-lifecycle-'))
    lifecycle = after[lifecycle_key]
    assert lifecycle['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
    for name in ['startup.sh', 'shutdown.sh']:
        assert lifecycle['data'][name].rstrip() == (chart / 'files' / name).read_text().rstrip()
    script_volume = next(v for v in new['template']['spec']['volumes'] if v['name'] == 'scripts')
    assert script_volume['configMap']['name'] == lifecycle_key[1]
    hook = after[('Job', 'cache-parallel-migration')]['spec']
    assert hook['activeDeadlineSeconds'] == 180 and hook['backoffLimit'] == 0
    assert '--cascade=orphan' in hook['template']['spec']['containers'][0]['args']
    # Regular release resources are unavailable to pre-upgrade hooks. The
    # migration must carry its own exact transport and write it to its tmp mount.
    hook_pod = hook['template']['spec']
    preparation = next(c for c in hook_pod['initContainers'] if c['name'] == 'prepare-hostnames')
    preparation_script = preparation['args'][0]
    assert (chart / 'files/transport.sh').read_text().rstrip() in preparation_script
    assert 'export REDIS_LIFECYCLE_DIR=/tmp' in preparation_script
    assert any(v['name'] == 'tmp' and v['mountPath'] == '/tmp' for v in preparation['volumeMounts'])
    assert not any('configMap' in v for v in hook_pod['volumes'])
    assert after[('Role', 'cache-parallel-migration')]['rules'] == [
        {'apiGroups': ['apps'], 'resources': ['statefulsets'], 'resourceNames': ['cache-node'], 'verbs': ['get', 'list', 'watch', 'delete']}]
    assert not any(key[0] == 'CiliumNetworkPolicy' for key in after)
    # Native quorum telemetry is optional and never changes the data Pod template.
    values['metrics'] = {'sentinel': {'enabled': True}}
    monitored = render(upgrade=True)
    assert monitored[('StatefulSet', 'cache-node')] == after[('StatefulSet', 'cache-node')]
    for index in range(3):
        pod = monitored[('Deployment', f'cache-sentinel-metrics-{index}')]['spec']['template']
        exporter = pod['spec']['containers'][0]
        assert exporter['args'] == [f'--redis.addr=redis://cache-node-{index}.cache-headless.database.svc.cluster.local:26379',
                                    '--include-config-metrics=false']
        assert exporter['env'][0]['valueFrom']['secretKeyRef'] == {'name': 'redis', 'key': 'redis-password'}
        assert pod['metadata']['annotations']['prometheus.io/job'] == 'redis-sentinel'
        assert not pod['spec']['automountServiceAccountToken']
    assert ('NetworkPolicy', 'cache-sentinel-metrics') in monitored
    values.pop('metrics')
    values['migration'] = {'ciliumNetworkPolicy': True}
    assert ('CiliumNetworkPolicy', 'cache-parallel-migration') in render(upgrade=True)
    values['fixtureStatefulSet'] = copy.deepcopy(values['fixtureStatefulSet'])
    values['fixtureStatefulSet']['spec']['podManagementPolicy'] = 'Parallel'
    assert ('Job', 'cache-parallel-migration') not in render(upgrade=True)
    values['fixtureStatefulSet'] = {}
    assert ('Job', 'cache-parallel-migration') not in render()
    # Secret payload and independent ownership survive explicitly requested adoption.
    secret = {'metadata': {'name': 'cache-auth', 'ownerReferences': [{'apiVersion': 'example.test/v1',
              'kind': 'SecretOwner', 'name': 'fixture', 'uid': 'fixture-owner'}]}, 'type': 'Opaque',
              'data': {'redis-password': 'Zml4dHVyZQ=='}}
    configs = {f'cache-{suffix}': {'metadata': {'name': f'cache-{suffix}'}, 'data': {'fixture': suffix}}
               for suffix in ['configuration', 'scripts', 'health']}
    values.update({'auth': {'existingSecret': 'cache-auth'}, 'fixtureLegacyObjects': {'Secret': {'cache-auth': secret}, 'ConfigMap': configs},
                   'migration': {'retainLegacyHelmSecret': True, 'retainLegacyConfigMaps': True}})
    retained = render()
    assert retained[('Secret', 'cache-auth')]['data'] == secret['data']
    assert retained[('Secret', 'cache-auth')]['metadata']['ownerReferences'] == secret['metadata']['ownerReferences']
    for name, item in configs.items():
        assert retained[('ConfigMap', name)]['data'] == item['data']
    (chart / 'files/startup.sh').write_text((chart / 'files/startup.sh').read_text() + '\n# fixture change\n')
    assert lifecycle_key not in render(), 'A lifecycle change must select a new retained ConfigMap'
    run('helm', 'lint', str(ROOT / 'charts/redis'), '--strict')
    invalid = subprocess.run(['helm', 'template', 'cache', str(chart), '--set', 'sentinel.downAfterMilliseconds=1000'],
                             capture_output=True, text=True)
    assert invalid.returncode != 0 and 'downAfterMilliseconds' in invalid.stderr
    custom = objects(run('helm', 'template', 'alternate', str(ROOT / 'charts/redis'), '--namespace', 'example',
                         '--set', 'clusterDomain=cluster.example'))
    env = custom[('StatefulSet', 'alternate-node')]['spec']['template']['spec']['containers'][0]['env']
    assert next(e['value'] for e in env if e['name'] == 'REDIS_HEADLESS') == 'alternate-headless.example.svc.cluster.example'
    enabled = objects(run('helm', 'template', 'bootstrap', str(ROOT / 'charts/redis'), '--set', 'bootstrap=true'))
    disabled = objects(run('helm', 'template', 'bootstrap', str(ROOT / 'charts/redis'), '--set', 'bootstrap=false'))
    assert enabled[('StatefulSet', 'bootstrap-node')] == disabled[('StatefulSet', 'bootstrap-node')]
    assert enabled[('ConfigMap', 'bootstrap-bootstrap')]['data']['enabled'] == 'yes'
    assert disabled[('ConfigMap', 'bootstrap-bootstrap')]['data']['enabled'] == 'no'
    for container in disabled[('StatefulSet', 'bootstrap-node')]['spec']['template']['spec']['containers'][:2]:
        assert container['command'][1] == '/health-bin/launcher.sh'
        for kind in ['startupProbe', 'readinessProbe', 'livenessProbe']:
            assert 'httpGet' in container[kind] and 'exec' not in container[kind]
    # Supported transport/storage modes preserve plaintext and PVC defaults.
    for tls in [False, True]:
        for persistent in [False, True]:
            settings = work / 'modes.json'
            settings.write_text(json.dumps({'tls': {'enabled': tls, 'existingSecret': 'fixture-tls'},
                'replica': {'persistence': {'enabled': persistent}, 'updateStrategy': 'OnDelete',
                            'minReplicasToWrite': 1, 'maxmemory': '256mb'},
                'networkPolicy': {'enabled': False}, 'metrics': {'sentinel': {'enabled': True}}}))
            mode = objects(run('helm', 'template', 'mode', str(ROOT / 'charts/redis'), '--namespace', 'example', '-f', str(settings)))
            spec = mode[('StatefulSet', 'mode-node')]['spec']
            assert spec['updateStrategy']['type'] == 'OnDelete'
            assert ('volumeClaimTemplates' in spec) == persistent
            assert any(v['name'] == 'redis-data' and v.get('emptyDir') == {} for v in spec['template']['spec']['volumes']) != persistent
            assert not any(k[0] == 'NetworkPolicy' for k in mode), 'Consumers can own the entire network boundary'
            config = mode[('ConfigMap', 'mode-runtime-config')]['data']
            assert ('appendonly yes' in config['redis.conf']) == persistent
            assert 'min-replicas-to-write 1' in config['redis.conf'] and 'maxmemory 256mb' in config['redis.conf']
            for target, port in [('redis', '6379'), ('sentinel', '26379')]:
                conf = config[target + '.conf']
                assert ('tls-port ' + port in conf) == tls
                if tls:
                    assert 'port 0' in conf and 'tls-replication yes' in conf
            if tls:
                exporter = spec['template']['spec']['containers'][2]
                assert '--redis.addr=rediss://127.0.0.1:6379' in exporter['args']
                tls_mounts = {v['name']:v for v in spec['template']['spec']['volumes']}
                assert tls_mounts['metrics-ca']['secret']['items'] == [{'key': 'ca.crt', 'path': 'ca.crt'}]
                assert 'tls' not in [m['name'] for m in exporter['volumeMounts']], 'Exporter must not receive server private key'
    metrics_settings = work / 'sentinel-metrics.json'
    metrics_settings.write_text(json.dumps({'auth': {'operator': {'username': 'operator'}, 'acl': {'existingSecret': 'acl'}},
        'metrics': {'sentinel': {'enabled': True, 'username': 'sentinel-metrics', 'existingSecret': 'metrics', 'passwordKey': 'sentinel-password'}}}))
    mode = objects(run('helm', 'template', 'mode', str(ROOT / 'charts/redis'), '-f', str(metrics_settings)))
    pod = mode[('StatefulSet', 'mode-node')]['spec']['template']['spec']
    credential = next(v for v in pod['volumes'] if v['name'] == 'sentinel-metrics-credential')
    assert credential['secret']['secretName'] == 'metrics'
    assert credential['secret']['items'] == [{'key': 'sentinel-password', 'path': 'password'}]
    sentinel = next(c for c in pod['containers'] if c['name'] == 'sentinel')
    assert next(e['value'] for e in sentinel['env'] if e['name'] == 'REDIS_SENTINEL_METRICS_USERNAME') == 'sentinel-metrics'
    collision = subprocess.run(['helm', 'template', 'mode', str(ROOT / 'charts/redis'), '-f', str(metrics_settings),
                                '--set', 'metrics.sentinel.username=operator'], capture_output=True, text=True)
    assert collision.returncode != 0 and 'Sentinel metrics identity must differ' in collision.stderr
    for arguments, message in [(['--set', 'tls.enabled=true'], 'tls.existingSecret'),
                               (['--set', 'replica.maxmemory=1mb\\ninjected'], 'maxmemory'),
                               (['--set', 'auth.discovery.username=default'], 'Discovery identity')]:
        invalid = subprocess.run(['helm', 'template', 'invalid', str(ROOT / 'charts/redis'), *arguments], capture_output=True, text=True)
        assert invalid.returncode != 0 and message in invalid.stderr, invalid.stderr
    print('PASS: self-contained lifecycle, retained identities/ownership, migration permissions/guards, configuration and schema')


if __name__ == '__main__':
    main()
