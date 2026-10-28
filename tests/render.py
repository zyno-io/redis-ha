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
    for previous, current in zip(old['template']['spec']['containers'], new['template']['spec']['containers']):
        for field in ['name', 'image', 'resources', 'securityContext', 'volumeMounts', 'lifecycle']:
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
    print('PASS: self-contained lifecycle, retained identities/ownership, migration permissions/guards, configuration and schema')


if __name__ == '__main__':
    main()
