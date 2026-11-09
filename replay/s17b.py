#!/usr/bin/env python3
"""s(17) B fresh-replay driver for ofitzharding/sq17-lean v1.0.0 (0a7167e).

Compiles the published sources with exactly the producer's commands (scripts/reproduce.py):
the five *Fast checker modules are compiled to shared libraries with leanc -O3, and every
other module is elaborated by `lean --load-dynlib=<all five> FILE -o OLEAN -i ILEAN`.
A module fails if lean exits non-zero or its log matches the producer's own failure regex
`error:|sorryAx|uses .sorry.`. Every compiled module gets a receipt line with the hashes of
its source, the oleans of its direct project imports, the native libraries and its own outputs,
so a later validator can check that one run built the whole closure from source.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tarfile
import time

FAST = ['HalfPlaneFast', 'CaptureRunFast', 'CaptureTableFast', 'HalfPlaneCoverFast', 'CaptureSummaryFast']
FINAL = 'Sq17Lean.Square17Optimality'
FAIL = re.compile(r'error:|sorryAx|uses .sorry.')
ROOT = Path.cwd()
LIB = ROOT / '.lake/build/lib/lean'
IR = ROOT / '.lake/build/ir/Sq17Lean'


class Fail(Exception):
    pass


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def manifest():
    m = json.loads((ROOT / 'manifest.json').read_text())
    mods = {v['module']: dict(v, path=k) for k, v in m['sources'].items()}
    if m['counts']['finalClosure'] != 4951 or len(mods) != 4953:
        raise Fail('manifest counts differ from v1.0.0')
    return m, mods


def graph(mods):
    deps = {k: [d for d in v['imports'] if d.startswith('Sq17Lean.')] for k, v in mods.items()}
    ext = {k: [d for d in v['imports'] if not d.startswith('Sq17Lean.')] for k, v in mods.items()}
    core, depth = {}, {}

    def visit(k):
        if k in depth:
            return
        for d in deps[k]:
            visit(d)
        depth[k] = 1 + max((depth[d] for d in deps[k]), default=0)
        core[k] = not ext[k] and all(core[d] for d in deps[k])
    sys.setrecursionlimit(100000)
    for k in mods:
        visit(k)
    return deps, core, depth


def closure(deps, roots):
    seen, stack = set(), list(roots)
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack += deps[x]
    return seen


def header_imports(p):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import reproduce
    return reproduce.imports(p)


def safe_dest(name):
    p = PurePosixPath(name)
    if not name.startswith('Sq17Lean/') or not name.endswith('.lean') or p.is_absolute() \
            or any(s in ('', '.', '..') for s in name.split('/')) or '\\' in name:
        raise Fail(f'unsafe archive member {name!r}')
    dest = ROOT.joinpath(*p.parts)
    for q in [dest, *dest.parents]:
        if q == ROOT:
            break
        if q.is_symlink():
            raise Fail(f'symlink destination {name}')
    return dest


def cmd_extract(a):
    """Authenticate archives and in-repo sources against manifest.json; extract the wanted modules."""
    m, mods = manifest()
    want = set(mods) if a.all else set(Path(a.modules).read_text().split())
    by_path = {v['path']: v for v in mods.values()}
    for name, info in m['files'].items():
        p = ROOT / name
        if not p.is_file() or sha(p) != info['sha256']:
            raise Fail(f'release file mismatch: {name}')
    for k, v in mods.items():
        if v['archive'] is None and sha(ROOT / v['path']) != v['sha256']:
            raise Fail(f'in-repo source mismatch: {k}')
    paths = {v['path'] for v in mods.values()}
    for f in (ROOT / 'Sq17Lean').rglob('*'):
        if f.is_symlink():
            raise Fail(f'symlink in source tree: {f}')
        if f.is_file() and f.suffix == '.lean' and str(f.relative_to(ROOT)) not in paths:
            raise Fail(f'unlisted source on disk: {f}')
    need = {mods[k]['archive'] for k in want if mods[k]['archive']}
    got = 0
    for arch, info in m['archives'].items():
        ap = Path(a.archives) / Path(arch).name
        if arch not in need:
            continue
        if ap.stat().st_size != info['bytes'] or sha(ap) != info['sha256']:
            raise Fail(f'archive mismatch {arch}')
        seen = set()
        with tarfile.open(ap, 'r:*') as t:
            for mem in t:
                if not mem.isfile():
                    raise Fail(f'non-file member {mem.name}')
                v = by_path.get(mem.name)
                if v is None or v['archive'] != arch:
                    raise Fail(f'unlisted member {mem.name} in {arch}')
                if mem.name in seen:
                    raise Fail(f'duplicate member {mem.name}')
                seen.add(mem.name)
                if v['module'] not in want:
                    continue
                dest = safe_dest(mem.name)
                data = t.extractfile(mem).read()
                if len(data) != v['bytes'] or hashlib.sha256(data).hexdigest() != v['sha256']:
                    raise Fail(f'member hash mismatch {mem.name}')
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix('.tmp')
                tmp.write_bytes(data)
                tmp.replace(dest)
                got += 1
        listed = {v['path'] for v in mods.values() if v['archive'] == arch}
        if seen != listed or len(seen) != info['members']:
            raise Fail(f'{arch}: member set differs from manifest')
        if a.delete_archives:
            ap.unlink()
    for k in want:
        hdr = header_imports(ROOT / mods[k]['path'])
        if hdr != mods[k]['imports']:
            raise Fail(f'header imports of {k} differ from manifest')
    print(json.dumps(dict(extracted=got, wanted=len(want), archives=sorted(need))))


def lean_bin():
    lean = Path(subprocess.check_output(['elan', 'which', 'lean'], text=True).strip())
    version = subprocess.check_output([lean, '--version'], text=True).strip()
    if '4.34.1' not in version:
        raise Fail(f'unexpected Lean version {version}')
    return lean, version


def timed(cmd, log, env=None):
    log.parent.mkdir(parents=True, exist_ok=True)
    tf = log.with_suffix('.time')
    t0 = time.time()
    with log.open('w') as f:
        r = subprocess.run(['/usr/bin/time', '-f', '%e %U %S %M', '-o', str(tf), *map(str, cmd)],
                           stdout=f, stderr=subprocess.STDOUT, env=env)
    wall, user, sys_, rss = tf.read_text().split()[-4:]
    tf.unlink()
    return r.returncode, dict(wall=float(wall), user=float(user), sys=float(sys_), maxrss_kb=int(rss),
                              t0=round(t0, 3))


def config_info():
    return {n: sha(ROOT / n) for n in ['manifest.json', 'lean-toolchain', 'lake-manifest.json', 'lakefile.toml',
                                       'replay/s17b.py', 'scripts/reproduce.py']}


def find_olean(lean_path, m):
    rel = m.replace('.', '/') + '.olean'
    for d in lean_path.split(os.pathsep):
        if d and (Path(d) / rel).is_file():
            return Path(d) / rel
    raise Fail(f'external import {m} not on LEAN_PATH')


def expected_oleans(receipt_dir):
    exp = {}
    for f in sorted(Path(receipt_dir).rglob('receipts.jsonl')):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get('kind') == 'fast' or (r.get('kind') == 'module' and r.get('ok')):
                if r['module'] in exp and exp[r['module']] != r['olean']:
                    raise Fail(f'two producers for {r["module"]}')
                exp[r['module']] = r['olean']
    return exp


def runner_info():
    return dict(host=platform.node(), cpus=os.cpu_count(), machine=platform.machine(),
                run=os.environ.get('GITHUB_RUN_ID'), attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
                job=os.environ.get('GITHUB_JOB'), shard=os.environ.get('S17_SHARD'),
                repo=os.environ.get('GITHUB_REPOSITORY'), sha=os.environ.get('GITHUB_SHA'))


def cmd_fast(a):
    lean, version = lean_bin()
    leanc = lean.with_name('leanc')
    out = Path(a.receipts)
    IR.mkdir(parents=True, exist_ok=True)
    (LIB / 'Sq17Lean').mkdir(parents=True, exist_ok=True)
    rec = []
    env = dict(os.environ, LEAN_PATH=str(LIB))
    for name in FAST:
        p = ROOT / 'Sq17Lean' / (name + '.lean')
        o = LIB / 'Sq17Lean' / (name + '.olean')
        log = Path(a.logs) / ('Sq17Lean.' + name + '.log')
        if o.exists():
            raise Fail(f'{name}: olean present before compiling')
        rc, t = timed([lean, p, '-o', o, '-i', o.with_suffix('.ilean'), '-c', IR / (name + '.c')], log, env)
        if rc or FAIL.search(log.read_text(errors='replace')):
            raise Fail(f'fast module {name} failed')
        subprocess.run([leanc, '-c', IR / (name + '.c'), '-o', IR / (name + '.o'), '-O3', '-DNDEBUG',
                        '-DLEAN_EXPORTING', '-fPIC'], check=True)
        so = LIB / ('Sq17Lean_' + name + '.so')
        subprocess.run([leanc, '-shared', '-o', so, IR / (name + '.o')], check=True)
        rec.append(dict(kind='fast', module='Sq17Lean.' + name, source=sha(p), olean=sha(o),
                        ilean=sha(o.with_suffix('.ilean')), c=sha(IR / (name + '.c')), so=sha(so),
                        log=sha(log), lean=version, config=config_info(), runner=runner_info(), **t))
    with out.open('a') as f:
        for r in rec:
            f.write(json.dumps(r, sort_keys=True) + '\n')
    print('FAST_OK', [r['so'][:12] for r in rec])


def native_libs():
    libs = [LIB / ('Sq17Lean_' + n + '.so') for n in FAST]
    for p in libs:
        if not p.is_file():
            raise Fail(f'missing native library {p}')
    return libs


def olean(m):
    return LIB / (m.replace('.', '/') + '.olean')


def cmd_build(a):
    m, mods = manifest()
    deps, core, depth = graph(mods)
    lean, version = lean_bin()
    targets = [x for x in Path(a.modules).read_text().split()]
    tset = set(targets)
    fast = {'Sq17Lean.' + n for n in FAST}
    if tset & fast:
        raise Fail('fast modules are built by the fast step')
    libs = native_libs()
    native_key = {p.name: sha(p) for p in libs}
    lean_path = str(LIB)
    if not all(core[t] for t in targets):
        lean_path += os.pathsep + subprocess.check_output(['lake', 'env', 'printenv', 'LEAN_PATH'], text=True).strip()
    env = dict(os.environ, LEAN_PATH=lean_path)
    expect = expected_oleans(a.expect) if a.expect else {}
    supplied = {d for t in targets for d in deps[t] if d not in tset}
    for d in sorted(supplied):
        if not olean(d).is_file():
            raise Fail(f'import {d} neither in this shard nor supplied')
        if a.expect and sha(olean(d)) != expect.get(d):
            raise Fail(f'supplied olean {d} does not match its producing receipt')
    config = config_info()
    ext_hash = {}
    for t in targets:
        for d in mods[t]['imports']:
            if not d.startswith('Sq17Lean.') and d not in ext_hash:
                ext_hash[d] = sha(find_olean(lean_path, d))
    logs = Path(a.logs)
    rec_f = Path(a.receipts).open('a')
    done = {d for t in targets for d in deps[t] if d not in tset}
    failed = []

    def one(t):
        p = ROOT / mods[t]['path']
        o = olean(t)
        o.parent.mkdir(parents=True, exist_ok=True)
        if o.exists():
            raise Fail(f'{t}: olean already present before compiling')
        dep_hash = {d: sha(olean(d)) for d in deps[t]}
        log = logs / (t + '.log')
        rc, tm = timed([lean, *['--load-dynlib=' + str(n) for n in libs], p, '-o', o, '-i', o.with_suffix('.ilean')],
                       log, env)
        text = log.read_text(errors='replace')
        ok = rc == 0 and not FAIL.search(text) and o.is_file()
        r = dict(kind='module', module=t, ok=ok, rc=rc, source=sha(p), source_manifest=mods[t]['sha256'],
                 imports=dep_hash, native=native_key, lean=version, log=sha(log), config=config,
                 external={d: ext_hash[d] for d in mods[t]['imports'] if not d.startswith('Sq17Lean.')},
                 argv=['lean', *['--load-dynlib=' + n.name for n in libs], mods[t]['path']],
                 olean=sha(o) if o.is_file() else None,
                 ilean=sha(o.with_suffix('.ilean')) if o.with_suffix('.ilean').is_file() else None,
                 olean_bytes=o.stat().st_size if o.is_file() else None, runner=runner_info(), **tm)
        if not ok:
            for q in (o, o.with_suffix('.ilean')):
                q.unlink(missing_ok=True)
        return r

    pending, running = set(targets), {}
    jobs, pjobs = a.jobs, a.proof_jobs
    with cf.ThreadPoolExecutor(jobs + pjobs) as pool:
        while pending or running:
            # Largest first among ready modules, bounded per class as in the producer's driver.
            for t in sorted(pending, key=lambda x: -mods[x]['bytes']):
                if not all(d in done for d in deps[t]):
                    continue
                c = sum(core[q] == core[t] for q in running.values())
                if c >= (jobs if core[t] else pjobs):
                    continue
                running[pool.submit(one, t)] = t
                pending.discard(t)
            if not running:
                raise Fail('unresolved: ' + ', '.join(sorted(pending)[:10]))
            fin, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for fu in fin:
                t = running.pop(fu)
                r = fu.result()
                rec_f.write(json.dumps(r, sort_keys=True) + '\n')
                rec_f.flush()
                print(('OK  ' if r['ok'] else 'FAIL'), t, r['wall'], r['maxrss_kb'], flush=True)
                if r['ok']:
                    done.add(t)
                else:
                    failed.append(t)
                    if not a.keep_going:
                        pending.clear()
            if failed and not a.keep_going and not running:
                break
    rec_f.close()
    if failed:
        raise Fail(f'{len(failed)} modules failed: {failed[:10]}')
    print('SHARD_BUILD_OK', len(targets))


def cmd_check(a):
    """leanchecker per-module replay: each module's own constants against its (olean) imports."""
    lean, version = lean_bin()
    checker = lean.with_name('leanchecker')
    targets = Path(a.modules).read_text().split()
    lean_path = str(LIB)
    if a.mathlib:
        lean_path += os.pathsep + subprocess.check_output(['lake', 'env', 'printenv', 'LEAN_PATH'], text=True).strip()
    env = dict(os.environ, LEAN_PATH=lean_path)
    out = Path(a.receipts).open('a')
    bad = []
    m, mods = manifest()
    deps, core, depth = graph(mods)
    batches, cur = [], []
    # Each target replays in its own task with its own imported environment: keep Mathlib-importing batches small.
    for t in sorted(targets, key=lambda x: not core[x]):
        cur.append(t)
        if len(cur) >= (a.batch if core[t] else a.mathlib_batch):
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    for i, batch in enumerate(batches):
        log = Path(a.logs) / f'leanchecker-{i:05d}.log'
        before = {t: sha(olean(t)) for t in batch}
        rc, tm = timed([checker, *batch], log, env)
        after = {t: sha(olean(t)) for t in batch}
        if before != after:
            raise Fail('olean changed during leanchecker')
        r = dict(kind='leanchecker', modules=batch, rc=rc, log=sha(log), lean=version,
                 olean=after, runner=runner_info(), **tm)
        out.write(json.dumps(r, sort_keys=True) + '\n')
        out.flush()
        print('CHECK', i, rc, tm['wall'], tm['maxrss_kb'], flush=True)
        if rc:
            bad.append(i)
    out.close()
    if bad:
        raise Fail(f'leanchecker failed in batches {bad}')
    print('LEANCHECKER_OK', len(targets))


def cmd_plan(a):
    """Write module lists for the given depth bands, bin-packed by source bytes into shards."""
    m, mods = manifest()
    deps, core, depth = graph(mods)
    clos = closure(deps, [FINAL])
    if len(clos) != 4951:
        raise Fail(f'closure has {len(clos)} modules')
    fast = {'Sq17Lean.' + n for n in FAST}
    plan = {}
    for spec in a.waves:
        name, band, n = spec.split(':')
        lo, hi = map(int, band.split('-'))
        n = int(n)
        mem = sorted(k for k in clos - fast if lo <= depth[k] <= hi)
        parent = {k: k for k in mem}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        ms = set(mem)
        for k in mem:
            for d in deps[k]:
                if d in ms:
                    parent[find(k)] = find(d)
        comps = {}
        for k in mem:
            comps.setdefault(find(k), []).append(k)
        bins = [[0, []] for _ in range(n)]
        for c in sorted(comps.values(), key=lambda c: -sum(mods[x]['bytes'] + a.overhead for x in c)):
            b = min(bins, key=lambda b: b[0])
            b[0] += sum(mods[x]['bytes'] + a.overhead for x in c)
            b[1] += c
        plan[name] = [dict(shard=f'{name}-{i:02d}', bytes=b[0], modules=sorted(b[1])) for i, b in enumerate(bins) if b[1]]
    covered = [x for w in plan.values() for s in w for x in s['modules']]
    if a.complete and set(covered) | fast != clos:
        raise Fail(f'plan covers {len(set(covered) | fast)} of {len(clos)}')
    if len(covered) != len(set(covered)):
        raise Fail('module in two shards')
    Path(a.out).write_text(json.dumps(plan, indent=1))
    for w, s in plan.items():
        print(w, len(s), [(x['shard'], len(x['modules']), x['bytes'] // 10**6) for x in s][:3])


def cmd_pilot(a):
    """Module lists for the pilot: the statement closure, and a heavy plus random depth-2 sample."""
    import random
    m, mods = manifest()
    deps, core, depth = graph(mods)
    fast = {'Sq17Lean.' + n for n in FAST}
    p1 = closure(deps, ['Sq17Lean.PackingStatement', 'Sq17Lean.EndpointGeometry']) - fast
    d2 = sorted((k for k in closure(deps, [FINAL]) if depth[k] == 2 and k not in p1), key=lambda k: -mods[k]['bytes'])
    rnd = random.Random(17).sample(d2[a.heavy:], a.random)
    p2 = closure(deps, d2[:a.heavy] + rnd) - fast - p1
    Path('pilot1.txt').write_text('\n'.join(sorted(p1)) + '\n')
    Path('pilot2.txt').write_text('\n'.join(sorted(p2)) + '\n')
    print('pilot1', len(p1), sum(mods[k]['bytes'] for k in p1), 'pilot2', len(p2), sum(mods[k]['bytes'] for k in p2))


def cmd_stage(a):
    """Copy this shard's own outputs (olean, ilean; .so for the fast step) under DEST, mirroring lib/lean."""
    import shutil
    names = Path(a.modules).read_text().split() if a.modules else []
    files = [olean(m) for m in names] + [olean(m).with_suffix('.ilean') for m in names]
    if a.fast:
        files += [olean('Sq17Lean.' + n) for n in FAST] + [olean('Sq17Lean.' + n).with_suffix('.ilean') for n in FAST]
        files += native_libs()
    missing = [f for f in files if not f.is_file()]
    if missing:
        raise Fail(f'stage: {len(missing)} outputs missing, first {missing[0]}')
    n = 0
    for f in files:
        d = Path(a.dest) / f.relative_to(LIB)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, d)
        n += 1
    print('STAGED', n, 'of', len(files))


def cmd_matrix(a):
    """GitHub outputs: one matrix JSON per wave from plan.json (shard, mathlib, job counts)."""
    m, mods = manifest()
    deps, core, depth = graph(mods)
    plan = json.loads(Path(a.plan).read_text())
    lines = []
    for w in a.waves:
        shards = [dict(shard=s['shard'], mathlib='0' if all(core[x] for x in s['modules']) else '1',
                       jobs=a.jobs, pjobs=a.pjobs) for s in plan.get(w, [])]
        lines.append(f'{w}={json.dumps(shards, separators=(",", ":"))}')
    print('\n'.join(lines))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('extract')
    e.add_argument('--modules')
    e.add_argument('--all', action='store_true')
    e.add_argument('--archives', default='certificates')
    e.add_argument('--delete-archives', action='store_true')
    f = sub.add_parser('fast')
    b = sub.add_parser('build')
    b.add_argument('--modules', required=True)
    b.add_argument('--jobs', type=int, default=4)
    b.add_argument('--proof-jobs', type=int, default=2)
    b.add_argument('--keep-going', action='store_true')
    b.add_argument('--expect', help='receipt dir of earlier waves; every supplied olean must match')
    c = sub.add_parser('check')
    c.add_argument('--modules', required=True)
    c.add_argument('--batch', type=int, default=40)
    c.add_argument('--mathlib', action='store_true')
    c.add_argument('--mathlib-batch', type=int, default=6)
    for x in (f, b, c):
        x.add_argument('--receipts', required=True)
        x.add_argument('--logs', required=True)
    pl = sub.add_parser('plan')
    pl.add_argument('--waves', nargs='+', required=True, help='NAME:LO-HI:SHARDS')
    pl.add_argument('--overhead', type=int, default=20000)
    pl.add_argument('--complete', action='store_true')
    pl.add_argument('--out', required=True)
    mx = sub.add_parser('matrix')
    mx.add_argument('--plan', required=True)
    mx.add_argument('--waves', nargs='+', required=True)
    mx.add_argument('--jobs', type=int, required=True)
    mx.add_argument('--pjobs', type=int, required=True)
    st = sub.add_parser('stage')
    st.add_argument('--modules')
    st.add_argument('--fast', action='store_true')
    st.add_argument('--dest', required=True)
    pp = sub.add_parser('pilot')
    pp.add_argument('--heavy', type=int, default=10)
    pp.add_argument('--random', type=int, default=60)
    a = p.parse_args()
    try:
        dict(extract=cmd_extract, fast=cmd_fast, build=cmd_build, check=cmd_check, plan=cmd_plan, pilot=cmd_pilot, stage=cmd_stage, matrix=cmd_matrix)[a.cmd](a)
    except Fail as x:
        print('S17B_FAIL', x, file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
