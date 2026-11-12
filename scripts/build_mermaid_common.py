"""Bundle common Mermaid diagrams into one request; keep upstream modules as fallback.

Install esbuild 0.25.12 in a local tools directory, then pass its binary with
--esbuild. The upstream versioned modules are never modified. Change the output
version before publishing changed bytes, since the browser caches it immutably.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
UPSTREAM = PROJECT / 'relayctl/static/vendor/mermaid-12.3.0'


def build(esbuild, output):
    tool = str(Path(esbuild).resolve()) if '/' in esbuild else esbuild
    assert subprocess.check_output([tool, '--version'], text=True).strip() == '0.25.12', 'Use esbuild 0.25.12 for reproducible assets'
    common = re.compile(r'^(flowDiagram|sequenceDiagram|dagre)-')
    def optional(match):
        if common.match(Path(match.group(1)).name):
            return match.group(0)
        return '(globalThis.relayMermaidOptional=true,Promise.reject(new Error("Optional Mermaid module")))'
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for source in UPSTREAM.rglob('*.mjs'):
            if source.name.startswith('relay-common-'):
                continue
            path = root / source.relative_to(UPSTREAM)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(re.sub(r'import\("(\./[^"\n]+\.mjs)"\)', optional, source.read_text()))
        subprocess.run([tool, str(root / 'mermaid.esm.min.mjs'), '--bundle', '--minify', '--format=esm', '--target=es2020', '--legal-comments=inline', '--outfile=' + str(output.resolve())], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--esbuild', default='esbuild')
    parser.add_argument('--output', type=Path, default=UPSTREAM / 'relay-common-v1.mjs')
    args = parser.parse_args()
    build(args.esbuild, args.output)
