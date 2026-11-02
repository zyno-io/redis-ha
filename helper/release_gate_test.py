#!/usr/bin/env python3
"""The release gate rejects tags and never reads the runner's Docker login."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='redis-health-gate-') as temp:
        work = Path(temp)
        binary = work / 'docker'
        calls = work / 'calls'
        caller_config = work / 'caller-auth'
        caller_config.mkdir()
        (caller_config / 'config.json').write_text('{"auths":{"fixture":{"auth":"fixture-only"}}}')
        binary.write_text('#!/bin/bash\n'
                          'test -d "$DOCKER_CONFIG" || exit 4\n'
                          'test "$(cat "$DOCKER_CONFIG/config.json")" = \'{"auths":{"ghcr.io":{}}}\' || exit 5\n'
                          'test -z "${DOCKER_AUTH_CONFIG+x}" && test -z "${REGISTRY_AUTH_FILE+x}" || exit 6\n'
                          'printf "%s\\n" "$*" >> "$GATE_TEST_CALLS"\n'
                          'if [[ "$1" == manifest ]]; then\n'
                          '  [[ "${GATE_TEST_MANIFEST_FAILURE:-0}" == 0 ]] || exit 7\n'
                          '  if [[ "${GATE_TEST_MISSING_ARM:-0}" == 1 ]]; then cat "$GATE_TEST_MISSING_MANIFEST"; else cat "$GATE_TEST_MANIFEST"; fi\n'
                          'fi\n'
                          'if [[ "$1" == pull ]]; then exit "${GATE_TEST_DOCKER_FAILURE:-0}"; fi\n')
        binary.chmod(0o755)
        manifest = work / 'manifest.json'
        missing_manifest = work / 'missing.json'
        amd = 'sha256:' + 'c' * 64
        arm = 'sha256:' + 'd' * 64
        manifest.write_text('{"manifests":[{"digest":"' + amd + '","platform":{"os":"linux","architecture":"amd64"}},'
                            '{"digest":"' + arm + '","platform":{"os":"linux","architecture":"arm64"}}]}')
        missing_manifest.write_text('{"manifests":[{"digest":"' + amd + '","platform":{"os":"linux","architecture":"amd64"}}]}')
        environment = dict(os.environ, PATH=str(work) + ':' + os.environ['PATH'], DOCKER_CONFIG=str(caller_config),
                           DOCKER_AUTH_CONFIG='fixture-auth', REGISTRY_AUTH_FILE='fixture-file',
                           GATE_TEST_CALLS=str(calls), GATE_TEST_MANIFEST=str(manifest),
                           GATE_TEST_MISSING_MANIFEST=str(missing_manifest))
        valid = 'ghcr.io/zyno-io/redis-ha-health:' + 'a' * 40 + '@sha256:' + 'b' * 64

        def gate(image, env=None):
            return subprocess.run(['bash', str(ROOT / 'scripts/verify-health-image.sh'), image],
                                  env=env or environment, capture_output=True, text=True)

        for image in ['ghcr.io/zyno-io/redis-ha-health:0.3.0', 'ghcr.io/zyno-io/redis-ha-health:latest',
                      valid.replace('zyno-io', 'other-owner')]:
            assert gate(image).returncode != 0
        assert not calls.exists(), 'unqualified image caused a registry request'
        result = gate(valid)
        assert result.returncode == 0, result.stderr
        lines = calls.read_text().splitlines()
        assert len(lines) == 3 and lines[0].startswith('manifest inspect ')
        assert '--platform linux/amd64' in lines[1] and amd in lines[1]
        assert '--platform linux/arm64' in lines[2] and arm in lines[2]
        result = gate(valid, dict(environment, GATE_TEST_MISSING_ARM='1'))
        assert result.returncode != 0 and 'exactly one Linux arm64' in result.stderr
        result = gate(valid, dict(environment, GATE_TEST_DOCKER_FAILURE='7'))
        assert result.returncode != 0 and 'Verified public' not in result.stdout
        result = gate(valid, dict(environment, GATE_TEST_MANIFEST_FAILURE='1'))
        assert result.returncode != 0 and 'Verified public' not in result.stdout
        assert (caller_config / 'config.json').exists(), 'runner login was modified'
    print('PASS: release pin, anonymous config isolation, both architectures, pull-failure gate')


if __name__ == '__main__':
    main()
