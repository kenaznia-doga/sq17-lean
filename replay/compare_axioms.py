#!/usr/bin/env python3
"""Compare a Lean `#print axioms` log for Sq17.square17_optimality against a reference log.

usage: compare_axioms.py OURS.log [--reference PRODUCER.log] --out result.json

Exit 0 only on MATCH. ERROR = input unparseable; MISMATCH = parsed but policy/set/statement failure.
"""
import argparse
import hashlib
import json
import re
import sys

THEOREM = "Sq17.square17_optimality"
STATEMENT = "Sq17.square17_optimality (L : ℝ) : Sq17.HalfPlaneAssembly.PlainPacking L ↔ Sq17.EndpointGeometry.side ≤ L"
STANDARD = {"propext", "Classical.choice", "Quot.sound"}
NATIVE_RE = re.compile(r"^[A-Za-z0-9_.']+\._native\.native_decide\.ax_[0-9_]+$")
HEADER = "'" + THEOREM + "' depends on axioms: ["


class ParseError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_log(path):
    """Return (names_in_order, statement_line). Raises ParseError on anything unexpected."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    n_headers = text.count("depends on axioms:")
    if n_headers != 1:
        raise ParseError(f"expected exactly one 'depends on axioms:' entry, found {n_headers}")
    start = text.find(HEADER)
    if start < 0:
        raise ParseError(f"header for {THEOREM} not found")
    body_start = start + len(HEADER)
    end = text.find("]", body_start)
    if end < 0:
        raise ParseError("axiom list is not closed with ']'")
    raw = text[body_start:end]
    if "[" in raw or "\n\n" in raw:
        raise ParseError("malformed axiom list")
    names = [n.strip() for n in raw.replace("\n", " ").split(",")]
    if any(n == "" for n in names):
        raise ParseError("empty axiom name in list")
    for n in names:
        if re.search(r"\s", n):
            raise ParseError(f"whitespace inside axiom name: {n!r}")
    rest = text[end + 1:]
    tail_lines = [ln for ln in rest.split("\n") if ln.strip() != ""]
    if not tail_lines:
        raise ParseError("no statement line after axiom list")
    statement = tail_lines[0].rstrip("\r")
    return names, statement


def classify(names):
    counts = {"standard": 0, "native": 0, "other": 0}
    other, native, standard = [], [], []
    for n in names:
        if n in STANDARD:
            counts["standard"] += 1
            standard.append(n)
        elif NATIVE_RE.match(n):
            counts["native"] += 1
            native.append(n)
        else:
            counts["other"] += 1
            other.append(n)
    return counts, standard, native, other


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ours")
    ap.add_argument("--reference")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    result = {
        "status": "ERROR",
        "theorem": THEOREM,
        "counts": None,
        "missing": [],
        "extra": [],
        "other": [],
        "sorry": False,
        "duplicates": [],
        "statement_ok": False,
        "statement": None,
        "reference_counts": None,
        "problems": [],
        "sha256": {"ours": None, "reference": None},
        "inputs": {"ours": args.ours, "reference": args.reference},
    }

    def finish(code):
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(json.dumps({k: result[k] for k in ("status", "counts", "problems")}, ensure_ascii=False))
        return code

    try:
        result["sha256"]["ours"] = sha256_file(args.ours)
        if args.reference:
            result["sha256"]["reference"] = sha256_file(args.reference)
        ours_names, ours_stmt = parse_log(args.ours)
    except (OSError, ParseError, UnicodeDecodeError) as e:
        result["problems"].append(f"ours: {e}")
        return finish(2)

    counts, standard, native, other = classify(ours_names)
    result["counts"] = counts
    result["other"] = other
    result["statement"] = ours_stmt
    result["statement_ok"] = ours_stmt == STATEMENT
    result["sorry"] = any("sorryAx" in n for n in ours_names)
    seen, dups = set(), []
    for n in ours_names:
        if n in seen and n not in dups:
            dups.append(n)
        seen.add(n)
    result["duplicates"] = dups

    problems = []
    if other:
        problems.append(f"{len(other)} OTHER axiom(s) not standard/native")
    if result["sorry"]:
        problems.append("sorryAx present")
    if dups:
        problems.append(f"{len(dups)} duplicate axiom(s)")
    if not result["statement_ok"]:
        problems.append("statement line differs from expected")
    if len(native) == 0:
        problems.append("no native_decide axioms found (unexpected)")

    ref_set = None
    if args.reference:
        try:
            ref_names, _ref_stmt = parse_log(args.reference)
        except (OSError, ParseError, UnicodeDecodeError) as e:
            result["problems"].append(f"reference: {e}")
            return finish(2)
        rcounts, _, _, rother = classify(ref_names)
        result["reference_counts"] = rcounts
        if rother:
            result["problems"].append(f"reference has {len(rother)} OTHER axiom(s); cannot compare")
            return finish(2)
        ref_set = set(ref_names)
        our_set = set(ours_names)
        result["missing"] = sorted(ref_set - our_set)
        result["extra"] = sorted(our_set - ref_set)
        if result["missing"]:
            problems.append(f"{len(result['missing'])} reference axiom(s) missing")
        if result["extra"]:
            problems.append(f"{len(result['extra'])} extra axiom(s) not in reference")

    result["problems"] = problems
    result["status"] = "MATCH" if not problems else "MISMATCH"
    return finish(0 if result["status"] == "MATCH" else 1)


if __name__ == "__main__":
    sys.exit(main())
