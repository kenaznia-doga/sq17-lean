#!/usr/bin/env python3
"""Fail-closed validator for one s(17) B fresh-replay workflow run.

Accepts only if every one of the 4,951 modules in the import closure of
Sq17Lean.Square17Optimality was compiled exactly once successfully in the given run,
from the source bytes listed in the producer's manifest.json, every recorded import
olean hash equals the olean produced for that import in the same run, all modules saw
the same five native libraries and Lean 4.34.1, every module was replayed by
leanchecker (exit 0) on the same olean bytes, and (optionally) the olean files on
disk hash to the receipts. Usage:
  check17.py --manifest manifest.json --receipts DIR --run RUN_ID [--lib LIBDIR] --out result.json
DIR is searched recursively for receipts.jsonl / check.jsonl files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

FAST = ['HalfPlaneFast', 'CaptureRunFast', 'CaptureTableFast', 'HalfPlaneCoverFast', 'CaptureSummaryFast']
FINAL = 'Sq17Lean.Square17Optimality'
REPO = 'kenaznia-doga/sq17-lean'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def validate(manifest, receipt_dir, run, lib):
    errors = []

    def err(code, detail):
        errors.append(dict(code=code, detail=detail))

    mods = {v['module']: v for v in manifest['sources'].values()}
    deps = {k: [d for d in v['imports'] if d.startswith('Sq17Lean.')] for k, v in mods.items()}
    clos, stack = set(), [FINAL]
    while stack:
        x = stack.pop()
        if x not in clos:
            clos.add(x)
            stack += deps[x]
    if len(clos) != 4951:
        err('closure_size', len(clos))
    fast = {'Sq17Lean.' + n for n in FAST}
    built, checked, fast_rec = {}, {}, {}
    files = sorted(Path(receipt_dir).rglob('receipts.jsonl')) + sorted(Path(receipt_dir).rglob('check.jsonl'))
    if not files:
        err('no_receipts', str(receipt_dir))
    versions, natives, shas = set(), set(), set()
    for f in files:
        for i, line in enumerate(f.read_text().splitlines()):
            try:
                r = json.loads(line)
            except ValueError:
                err('bad_json', f'{f}:{i + 1}')
                continue
            ru = r.get('runner', {})
            if str(ru.get('run')) != str(run) or ru.get('repo') != REPO:
                err('foreign_receipt', f'{f}:{i + 1} run={ru.get("run")} repo={ru.get("repo")}')
                continue
            shas.add(ru.get('sha'))
            versions.add(r.get('lean'))
            k = r.get('kind')
            if k == 'fast':
                if r['module'] in fast_rec:
                    err('fast_twice', r['module'])
                fast_rec[r['module']] = r
            elif k == 'module':
                if not r.get('ok'):
                    continue
                if r['module'] in built:
                    err('built_twice', r['module'])
                built[r['module']] = r
                natives.add(json.dumps(r['native'], sort_keys=True))
            elif k == 'leanchecker':
                if r.get('rc') == 0:
                    for m, h in r['olean'].items():
                        checked.setdefault(m, set()).add(h)
            else:
                err('unknown_kind', f'{f}:{i + 1}')
    if len(versions) != 1 or not next(iter(versions), '') or '4.34.1' not in next(iter(versions)):
        err('lean_version', sorted(map(str, versions)))
    if len(shas) != 1:
        err('commit', sorted(map(str, shas)))
    if set(fast_rec) != fast:
        err('fast_set', sorted(fast ^ set(fast_rec)))
    so = {('Sq17Lean_' + r['module'].split('.')[-1] + '.so'): r['so'] for r in fast_rec.values()}
    if natives != {json.dumps(so, sort_keys=True)}:
        err('native_libs', f'{len(natives)} distinct native sets; expected the fast step libraries')
    olean = {m: r['olean'] for m, r in {**fast_rec, **built}.items()}
    for m, r in fast_rec.items():
        if r['source'] != mods[m]['sha256']:
            err('source_hash', m)
    missing = sorted(clos - fast - set(built))
    if missing:
        err('missing_modules', dict(count=len(missing), first=missing[:20]))
    extra = sorted(set(built) - clos)
    if extra:
        err('outside_closure', extra[:20])
    for m, r in built.items():
        if m not in mods:
            continue
        if r['source'] != mods[m]['sha256'] or r['source_manifest'] != mods[m]['sha256']:
            err('source_hash', m)
        if sorted(r['imports']) != sorted(deps[m]):
            err('import_set', m)
            continue
        for d, h in r['imports'].items():
            if olean.get(d) != h:
                err('import_hash', f'{m} -> {d}')
        if r.get('rc') != 0 or not r.get('olean'):
            err('bad_ok_receipt', m)
    unchecked = sorted(m for m in clos if olean.get(m) not in checked.get(m, set()))
    if unchecked:
        err('leanchecker_missing', dict(count=len(unchecked), first=unchecked[:20]))
    disk = None
    if lib:
        bad = []
        for m in sorted(clos):
            p = Path(lib) / (m.replace('.', '/') + '.olean')
            if not p.is_file() or sha(p) != olean.get(m):
                bad.append(m)
        disk = dict(checked=len(clos), mismatched=len(bad))
        if bad:
            err('disk_olean', dict(count=len(bad), first=bad[:20]))
    cpu = sum(r['user'] + r['sys'] for r in [*built.values(), *fast_rec.values()])
    return dict(status='FRESH_CHAIN_ACCEPTED' if not errors else 'REJECTED', run=str(run), repo=REPO,
                commit=next(iter(shas), None), lean=sorted(map(str, versions)), modules_built=len(built) + len(fast_rec),
                closure=len(clos), leanchecked=len(clos) - len(unchecked), native_libs=so, disk=disk,
                cpu_hours=round(cpu / 3600, 2), final_olean=olean.get(FINAL), errors=errors[:200],
                error_count=len(errors), receipt_files=len(files))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', required=True)
    p.add_argument('--receipts', required=True)
    p.add_argument('--run', required=True)
    p.add_argument('--lib')
    p.add_argument('--out', required=True)
    a = p.parse_args()
    res = validate(json.loads(Path(a.manifest).read_text()), a.receipts, a.run, a.lib)
    Path(a.out).write_text(json.dumps(res, indent=1, sort_keys=True) + '\n')
    print(res['status'], res['error_count'], 'errors;', res['modules_built'], 'built;', res['leanchecked'], 'leanchecked')
    sys.exit(0 if res['status'] == 'FRESH_CHAIN_ACCEPTED' else 1)


if __name__ == '__main__':
    main()
