"""Build the four self-contained device clients and an atomic download manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def build(output, version):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}', version):
        raise ValueError('Invalid release version')
    output.mkdir(parents=True, exist_ok=True)
    release = output / version
    if release.exists():
        raise FileExistsError('Release versions are immutable: ' + str(release))
    with tempfile.TemporaryDirectory(dir=output, prefix='.build-') as folder:
        staging = Path(folder)
        assets = []
        for system in ('darwin', 'linux'):
            for arch in ('amd64', 'arm64'):
                name = 'relayctl-' + system + '-' + arch
                target = staging / name
                subprocess.run(['go', 'build', '-trimpath', '-ldflags',
                    '-s -w -X dev.relay/agent-manager/internal/relay.Version=' + version,
                    '-o', str(target), './cmd/relayctl'], cwd=PROJECT,
                    env=dict(os.environ, CGO_ENABLED='0', GOOS=system, GOARCH=arch), check=True)
                assets.append({'name': name, 'platform': system, 'arch': arch,
                    'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'size': target.stat().st_size,
                    'url': '/downloads/' + version + '/' + name})
        (staging / 'SHA256SUMS').write_text(''.join(a['sha256'] + '  ' + a['name'] + '\n' for a in assets))
        staging.chmod(0o755)
        os.rename(staging, release)
        # TemporaryDirectory tolerates the staging path moving away.
    manifest = {'version': version, 'checksums': '/downloads/' + version + '/SHA256SUMS', 'assets': assets}
    with tempfile.NamedTemporaryFile(mode='w', dir=output, prefix='.manifest-', delete=False) as f:
        json.dump(manifest, f, indent=2); f.write('\n')
        name = f.name
    os.chmod(name,0o644)
    os.replace(name, output / 'current.json')
    print(json.dumps({'version': version, 'output': str(release), 'platforms': len(assets)}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=PROJECT / 'dist/downloads')
    p.add_argument('--version', default='0.6.2')
    args = p.parse_args()
    build(args.output.resolve(), args.version)
