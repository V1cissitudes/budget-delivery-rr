"""Build --tasks-meta for bridge/batch.py (README item 18): {instance_id: {base_commit, problem_statement}} for the
100 sampled + 5 dev tasks, from the locally cached SWE-bench Verified test split (offline; no download). Cross-checks
base_commit against d1's base_commits.json when given. Not part of the bridge code identity (tools/); batch.py records
the sha256 of the produced file in its ledger.
Desktop: HF_DATASETS_OFFLINE=1 ~/code/d1-pilot/.venv/bin/python bridge/tools/make_tasks_meta.py \
           --selection docs/prereg/2026-10-18_bridge_selection_DRAFT.json --base-commits ~/code/d1-pilot/base_commits.json \
           --out bridge_tasks_meta.json"""
import argparse, hashlib, json, os, sys


def build(rows, ids, base_commits=None):
    by = {r["instance_id"]: r for r in rows}
    missing = [i for i in ids if i not in by]
    if missing:
        raise SystemExit(f"not in dataset: {missing}")
    out, mism = {}, []
    for i in ids:
        r = by[i]
        if not r.get("base_commit") or not r.get("problem_statement"):
            raise SystemExit(f"{i}: empty base_commit or problem_statement")
        if base_commits and i in base_commits and base_commits[i] != r["base_commit"]:
            mism.append(i)
        out[i] = {"base_commit": r["base_commit"], "problem_statement": r["problem_statement"]}
    if mism:
        raise SystemExit(f"base_commit differs from d1 base_commits.json: {mism}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--base-commits", default=None)
    a = ap.parse_args()
    sel = json.load(open(a.selection))
    ids = list(sel["high_60"]) + list(sel["low_40"]) + list(sel["dev_5"])
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    from datasets import load_dataset
    rows = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    bc = json.load(open(a.base_commits)) if a.base_commits else None
    out = build(list(rows), ids, bc)
    json.dump(out, open(a.out, "w"), indent=1, sort_keys=True)
    print(json.dumps({"n": len(out), "checked_against_d1": sum(1 for i in out if bc and i in bc),
                      "sha256": hashlib.sha256(open(a.out, "rb").read()).hexdigest()}))


if __name__ == "__main__":
    main()
