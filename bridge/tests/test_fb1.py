"""F-b1 (prereg v4 §9, secondary sensitivity product) offline tests: the non-UTF-8 new-file filter on a real K3 patch,
the stray-process re-snapshot logic, evaluation only where F-v6 is unusable (F-v6 untouched), and the analysis section.
Run: python3 -I bridge/tests/test_fb1.py"""
import hashlib, json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fakes as FK  # noqa: E402
import identity as ID  # noqa: E402
import snapshot as SN  # noqa: E402
import evaluate as EV  # noqa: E402
REAL = os.path.join(os.path.dirname(ID.HERE), "data", "d1_k2_v4", "sphinx-doc__sphinx-10449", "snap_t50.patch")


def test_filter_on_real_k3_sphinx_patch():
    p = open(REAL, "rb").read()
    try:
        p.decode("utf-8"); raise AssertionError("expected the K3 patch to be non-UTF-8")
    except UnicodeDecodeError:
        pass
    out, dropped = SN.drop_new_non_utf8(p)
    assert dropped == ["repro/docs/_build/html/objects.inv"], dropped
    out.decode("utf-8")                                                            # now ingestible by the harness
    assert b"diff --git a/sphinx/ext/autodoc/typehints.py" in out and b"GIT binary patch" in out   # fix + ASCII binaries kept
    assert len(p) - len(out) == 594                                               # only that one section removed


def test_filter_keeps_modified_non_utf8_and_text():
    mod = b"diff --git a/x.bin b/x.bin\nindex 1..2 100644\n--- a/x.bin\n+++ b/x.bin\n@@ -1 +1 @@\n-\xff\n+\xfe\n"
    new_txt = b"diff --git a/n.py b/n.py\nnew file mode 100644\nindex 0..1\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
    out, dropped = SN.drop_new_non_utf8(mod + new_txt)
    assert out == mod + new_txt and dropped == []                                 # only NEW non-UTF-8 files are dropped


def test_fb1_product_paths():
    ok = SN.fb1_product(b"diff --git a/a b/a\n", True, [[], []])
    assert ok["valid"] and ok["same_as_fv6"] and not ok["killed"]
    calls = []
    def resnap():
        calls.append(1); return b"diff --git a/b b/b\n", True, [[], []]
    r = SN.fb1_product(b"stale", False, [["python manage.py runserver"], []], resnap=resnap)
    assert r["valid"] and r["killed"] and r["patch"] == b"diff --git a/b b/b\n" and r["procs_before_kill"][0] and calls
    assert not SN.fb1_product(b"x", False, [["p"], []])["valid"]                  # no re-snapshot possible
    assert not SN.fb1_product(b"x", False, [["p"], []], resnap=lambda: (b"y", False, [["p"], []]))["valid"]
    bad = SN.fb1_product(b"x", False, [["p"], []], resnap=lambda: (_ for _ in ()).throw(RuntimeError("docker gone")))
    assert not bad["valid"] and "docker gone" in bad["reason"]


def _record_with_fb1(tdir, F, Fb1, **kw):
    FK.write_record(tdir, iid="x__y-1", arm="silent", attempt="a1", F=F, **kw)
    rec = json.load(open(os.path.join(tdir, "record.json")))
    open(os.path.join(tdir, "F_b1.patch"), "wb").write(Fb1)
    rec["F_b1"] = {"available": True, "sha256": hashlib.sha256(Fb1).hexdigest(), "bytes": len(Fb1), "killed": F is None}
    json.dump(rec, open(os.path.join(tdir, "record.json"), "w"))


def test_eval_uses_fb1_only_when_fv6_unusable():
    j = FK.FakeJudge()
    t = FK.tmpdir()                                                                 # stray process at the cap: F-v6 invalid
    _record_with_fb1(t, None, b"diff FIX", exit_status="LimitsExceeded", limit_kind="step")
    r = EV.evaluate(t, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=j)
    assert (r["code"], r["S"], r["H"], r["W"]) == ("CAP_STEP_DIFF", 0, None, None)  # primary (F-v6) unchanged: unknown
    assert (r["H_b1"], r["W_b1"], r["F_b1_used"]) == (1, 1, True)
    t2 = FK.tmpdir()                                                                # F-v6 unevaluable (judge unknown)
    _record_with_fb1(t2, b"diff FLAKY", b"diff FIX filtered", exit_status="LimitsExceeded", limit_kind="step")
    r2 = EV.evaluate(t2, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=j)
    assert r2["H"] is None and r2["H_b1"] == 1 and "F_b1" in r2["verdicts"]
    n = len(j.calls); t3 = FK.tmpdir()                                              # F-v6 fine: F-b1 never judged
    _record_with_fb1(t3, b"diff FIX", b"diff FIX", exit_status="LimitsExceeded", limit_kind="step")
    r3 = EV.evaluate(t3, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=j)
    assert (r3["H"], r3["H_b1"], r3["F_b1_used"]) == (1, 1, False) and len(j.calls) == n + 1
    t4 = FK.tmpdir()                                                                # submitted run: S/H untouched, W from F-b1
    _record_with_fb1(t4, None, b"diff nothing", exit_status="Submitted", submission="diff FIX")
    r4 = EV.evaluate(t4, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=j)
    assert (r4["S"], r4["H"], r4["W"], r4["H_b1"], r4["W_b1"]) == (1, 1, None, 1, 0)


def test_tampered_fb1_is_ignored_not_fatal():
    t = FK.tmpdir()
    _record_with_fb1(t, None, b"diff FIX", exit_status="LimitsExceeded", limit_kind="step")
    open(os.path.join(t, "F_b1.patch"), "wb").write(b"diff tampered")
    r = EV.evaluate(t, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert "F_B1_ARTIFACT_INCONSISTENT" in r["why"] and r["code"] == "CAP_STEP_DIFF" and r["H_b1"] is None


class _FakeEnv:
    container_id = "c0"


def _docker_snapshotter(tmp, procs_seq, resnap_calls):
    """DockerSnapshotter with Docker replaced by fakes (no subprocess)."""
    class FakeEX:
        @staticmethod
        def extract(repo, base_tree, objdir):
            p = b"diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
            return {"patch": p, "sha256": hashlib.sha256(p).hexdigest(), "flags": {}}
    sn = SN.DockerSnapshotter("/testbed", tmp, "base")
    sn.t0_tree = "t0"
    seq = iter(procs_seq)
    sn._procs = lambda cid: next(seq)
    sn._copy = lambda cid, d: d
    sn._kill_and_resnap = lambda cid: (resnap_calls.append(1), (b"diff --git a/y b/y\n", True, [[], []]))[1]
    return sn, FakeEX


def test_fb1_save_failure_never_loses_fv6():
    # review C1-1: writing F_b1 fails (a directory sits where the file should go); F-v6 must still be returned
    tmp = FK.tmpdir(); calls = []
    sn, fx = _docker_snapshotter(tmp, [[], []], calls)
    old = SN.EX; SN.EX = fx
    try:
        os.makedirs(os.path.join(tmp, "F_b1_1.patch"))
        r = sn(_FakeEnv())
    finally:
        SN.EX = old
    assert r["valid"] and os.path.exists(r["path"]) and r["procs"] == [[], []]
    assert r["b1"]["valid"] is False and "F-b1 failed" in r["b1"]["reason"]


def test_failed_process_check_never_kills():
    # review C1-2: docker top failed (None): F-v6 stays invalid (sealed rule), F-b1 unavailable, nothing killed
    tmp = FK.tmpdir(); calls = []
    sn, fx = _docker_snapshotter(tmp, [None, None], calls)
    old = SN.EX; SN.EX = fx
    try:
        r = sn(_FakeEnv())
    finally:
        SN.EX = old
    assert r["valid"] is False and calls == [] and r["b1"]["valid"] is False and "not confirmed" in r["b1"]["reason"]
    assert SN.stray_confirmed([["sleep 600"], []]) and not SN.stray_confirmed([None, []]) and not SN.stray_confirmed([[], []])
    tmp2 = FK.tmpdir(); calls2 = []
    sn2, fx2 = _docker_snapshotter(tmp2, [["sleep 600"], ["sleep 600"]], calls2)
    SN.EX = fx2
    try:
        r2 = sn2(_FakeEnv())
    finally:
        SN.EX = old
    assert calls2 == [1] and r2["b1"]["valid"] and r2["b1"]["killed"]


def test_dropped_path_with_spaces_and_b_slash():
    sec = b"diff --git a/a b/x b/a b/x\nnew file mode 100644\nindex 0..1\n--- /dev/null\n+++ b/a b/x\n@@ -0,0 +1 @@\n+\xff\n"
    out, dropped = SN.drop_new_non_utf8(sec)
    assert out == b"" and dropped == ["a b/x"]
    q = b'diff --git "a/\\303\\251" "b/\\303\\251"\nnew file mode 100644\n--- /dev/null\n+++ "b/x"\n@@ -0,0 +1 @@\n+\xff\n'
    assert SN.drop_new_non_utf8(q)[1][0].startswith('"a/')


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
