"""离线测试用的假件（只用标准库）：假 judge、按 run_one.finalize 的字段写运行记录、临时 git 仓库。"""
import hashlib, json, os, subprocess, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import identity as ID  # noqa: E402
from outcome import classify  # noqa: E402

IMG = "sha256:" + "ab" * 32


class FakeJudge:
    """Same signature as d1 judge.judge. Verdict from the patch bytes: b'FIX' -> 1, b'FLAKY' -> unknown
    (EVAL_UNSTABLE), b'BOOM' -> raises (infrastructure), otherwise 0; zero-length -> 0 without a launch (as d1)."""
    def __init__(self):
        self.calls = []

    def __call__(self, instance_id, patch, tag, workdir, *, img, expected_id, week=None):
        if len(patch) == 0:
            return {"verdict": 0, "reason": None, "slots": ["empty"], "empty": True}
        self.calls.append({"instance_id": instance_id, "tag": tag, "patch": patch, "expected_id": expected_id})
        if b"BOOM" in patch:
            raise RuntimeError("docker daemon unreachable")
        if b"FLAKY" in patch:
            return {"verdict": None, "reason": "EVAL_UNSTABLE", "slots": []}
        return {"verdict": 1 if b"FIX" in patch else 0, "reason": None, "slots": []}


def write_record(tdir, *, iid, arm, attempt, exit_status, submission=None, F=None, limit_kind=None, why=(),
                 versions=("gemini-3-flash-preview",), image_id=IMG, dev=False, bridge_code=None, k=ID.K):
    """Writes what run_one.finalize writes (record.json + submission.patch + F_end.patch)."""
    os.makedirs(tdir, exist_ok=True)
    if F is not None:
        open(os.path.join(tdir, "F_end.patch"), "wb").write(F)
    if exit_status == "Submitted":
        open(os.path.join(tdir, "submission.patch"), "wb").write((submission or "").encode())
    why = list(why) + (["SPEND_STOP"] if exit_status == "SpendStop" else []) + ([] if exit_status else ["NO_EXIT_STATUS"])
    code, rule = classify(exit_status=exit_status, submission=submission, limit_kind=limit_kind, workspace_patch=F,
                          eng_fail=bool(why))
    rec = {"instance_id": iid, "arm": arm, "attempt": attempt, "k": k, "cost_limit": ID.COST_LIMIT, "dev": dev,
           "expected_image_id": image_id, "bridge_code_sha256": bridge_code or ID.bridge_code_sha256(),
           "d1_code_sha256": ID.d1_code_sha256(), "exit_status": exit_status, "eng_fail": bool(why), "why": why,
           "resource_stop": exit_status == "SpendStop", "code": code, "rule": rule, "limit_kind": limit_kind,
           "n_calls": 7, "F": {"available": F is not None, "sha256": hashlib.sha256(F).hexdigest() if F is not None else None},
           "submission_sha256": hashlib.sha256((submission or "").encode()).hexdigest() if exit_status == "Submitted" else None,
           "budget_log": [{"n_calls": 1, "consistent": True}], "model_versions": list(versions), "terminal": True}
    json.dump(rec, open(os.path.join(tdir, "record.json"), "w"))
    return rec


def tmpdir():
    return tempfile.mkdtemp(prefix="bridge-test-")


def d1_snapshot():
    """Names, sizes and mtimes of every file under d1/ (to prove the tests never write there)."""
    out = {}
    for root, _, files in os.walk(ID.D1_DIR):
        for f in files:
            p = os.path.join(root, f); st = os.stat(p)
            out[os.path.relpath(p, ID.D1_DIR)] = (st.st_size, st.st_mtime_ns)
    return out


def run_tests(g):
    import traceback
    fails = n = 0
    for name, f in sorted(g.items()):
        if name.startswith("test_") and callable(f):
            n += 1
            try:
                f(); print("ok  ", name)
            except Exception:
                fails += 1; print("FAIL", name); traceback.print_exc()
    print(f"{n - fails}/{n} passed"); sys.exit(1 if fails else 0)
