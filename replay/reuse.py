#!/usr/bin/env python3
"""Reuse of earlier runs' completed shards by a continuation run (custody via receipts).

prep:  bases.json = [{run, shards:[globs]}] -> reuse/bases.json (resolved shard names, module lists,
       artifact ids/digests, head_sha) and the shard names the continuation must skip.
fetch: download the reused olean-/receipts- artifacts of every base into a lib dir / receipts dir.
"""
import argparse
import fnmatch
import json
import subprocess
from pathlib import Path

REPO = 'kenaznia-doga/sq17-lean'
FAST = ['HalfPlaneFast', 'CaptureRunFast', 'CaptureTableFast', 'HalfPlaneCoverFast', 'CaptureSummaryFast']


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def prep(a):
    new = json.loads(Path(a.plan).read_text())
    out, skip = [], []
    for i, b in enumerate(json.loads(a.bases)):
        run = str(b['run'])
        d = Path(a.out) / f'plan-{run}'
        d.mkdir(parents=True, exist_ok=True)
        gh('run', 'download', run, '--repo', REPO, '-n', 'plan', '-D', str(d))
        old = json.loads((d / 'plan.json').read_text())
        arts = {x['name']: x for x in (json.loads(l) for l in gh('api', '--paginate', f'repos/{REPO}/actions/runs/{run}/artifacts', '-q', '.artifacts[]').splitlines() if l.strip())}
        shards, mods = [], []
        newshards = {s['shard']: s['modules'] for w in new.values() for s in w}
        for w in old.values():
            for s in w:
                if any(fnmatch.fnmatch(s['shard'], g) for g in b['shards']):
                    assert newshards.get(s['shard']) == s['modules'], f'plan differs for {s["shard"]}'
                    for n in ('olean-', 'receipts-'):
                        x = arts.get(n + s['shard'])
                        assert x and not x['expired'], f'missing artifact {n}{s["shard"]} in {run}'
                    shards.append(s['shard'])
                    mods += s['modules']
        assert shards, f'base {run} matches no shard'
        if any(s.startswith('w0') for s in shards):
            mods += ['Sq17Lean.' + f for f in FAST]
        head = json.loads(gh('api', f'repos/{REPO}/actions/runs/{run}'))
        keep = [{k: arts[n + s][k] for k in ('id', 'name', 'size_in_bytes', 'digest', 'created_at')} for s in shards for n in ('olean-', 'receipts-')]
        out.append(dict(run=run, head_sha=head['head_sha'], shards=sorted(shards), modules=sorted(mods), artifacts=keep))
        skip += shards
    assert len(set(skip)) == len(skip), 'a shard is reused from two bases'
    assert len({m for b in out for m in b['modules']}) == sum(len(b['modules']) for b in out)
    Path(a.out, 'bases.json').write_text(json.dumps(out, indent=1))
    Path(a.out, 'skip.txt').write_text('\n'.join(sorted(skip)) + '\n')
    print(len(skip), 'shards reused from', [(b['run'], len(b['shards'])) for b in out])


def fetch(a):
    for b in json.loads(Path(a.reuse, 'bases.json').read_text()):
        for i in range(5):
            subprocess.run(['rm', '-rf', 'base-dl'])
            pats = [p for s in b['shards'] for p in ('--pattern', f'olean-{s}', '--pattern', f'receipts-{s}')]
            if subprocess.run(['gh', 'run', 'download', b['run'], '--repo', REPO, *pats, '-D', 'base-dl']).returncode == 0:
                break
            subprocess.run(['sleep', '30'])
        else:
            raise SystemExit('download failed')
        Path(a.lib).mkdir(parents=True, exist_ok=True)
        Path(a.receipts).mkdir(parents=True, exist_ok=True)
        for d in sorted(Path('base-dl').iterdir()):
            if d.name.startswith('olean-'):
                subprocess.check_call(['cp', '-rn', f'{d}/.', a.lib])
            else:
                assert not (Path(a.receipts) / d.name).exists()
                d.rename(Path(a.receipts) / d.name)
        subprocess.check_call(['rm', '-rf', 'base-dl'])


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest='cmd', required=True)
    x = s.add_parser('prep')
    x.add_argument('--bases', required=True)
    x.add_argument('--plan', required=True)
    x.add_argument('--out', required=True)
    y = s.add_parser('fetch')
    y.add_argument('--reuse', required=True)
    y.add_argument('--lib', required=True)
    y.add_argument('--receipts', required=True)
    a = p.parse_args()
    dict(prep=prep, fetch=fetch)[a.cmd](a)


if __name__ == '__main__':
    main()
