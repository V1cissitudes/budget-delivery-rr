"""批量驱动的离线测试（假 run_fn、假 judge、真 evaluate、真 spend 账本）。Run: python3 -I bridge/tests/test_batch.py"""
import glob, hashlib, itertools, json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes as FK  # noqa: E402
import batch as BA  # noqa: E402
import evaluate as EV  # noqa: E402
import prompts as P  # noqa: E402
from spend import Spend, reconcile  # noqa: E402

ARMS = list(P.ARMS)
SEL = os.path.join(os.path.dirname(FK.ID.HERE), "docs", "prereg", "2026-10-18_bridge_selection_DRAFT.json")


class Script:
    """run_fn: per (iid, arm) a list of per-attempt specs; default = submitted fix. Spec 'crash' writes nothing,
    'raise' raises; a dict goes to fakes.write_record; 'usd' is added to the spend ledger like the agent would."""
    def __init__(self, spend, plan=None, default=None, bridge_code=None):
        self.spend, self.plan, self.calls, self.bridge_code = spend, plan or {}, [], bridge_code
        self.default = default or {"exit_status": "Submitted", "submission": "diff FIX", "F": b"diff FIX"}

    def __call__(self, *, iid, arm, attempt, tdir, image_id):
        n = sum(1 for c in self.calls if c[:2] == (iid, arm))
        self.calls.append((iid, arm, attempt))
        specs = self.plan.get((iid, arm), [])
        spec = specs[n] if n < len(specs) else self.default
        if spec == "crash":
            return 1
        if spec == "raise":
            raise RuntimeError("subprocess launch failed")
        spec = dict(spec)
        usd = spec.pop("usd", 0.0)
        if usd:
            self.spend.add(usd)
        FK.write_record(tdir, iid=iid, arm=arm, attempt=attempt, image_id=image_id, bridge_code=self.bridge_code, **spec)
        return 0


def mk(ids, plan=None, stop=1000.0, not_ready=(), default=None, out=None, judge=None, disk_free=None, prior=None,
       bridge_code=None):
    out = out or FK.tmpdir()
    sp = Spend(os.path.join(out, "spend.json"), stop)
    run = Script(sp, plan, default, bridge_code=bridge_code); judge = judge or FK.FakeJudge()
    b = BA.Batch(out=out, ids=ids, arms=ARMS, pinned={i: {"pinned_id": FK.IMG} for i in ids}, spend=sp, run_fn=run,
                 eval_fn=lambda tdir, **kw: EV.evaluate(tdir, judge_fn=judge, bridge_code=bridge_code, **kw),   # as main()
                 image_id_fn=lambda iid: None if iid in not_ready else FK.IMG,
                 disk_free_fn=disk_free or (lambda: 1e12), prior=prior, bridge_code=bridge_code)
    return b, run, out


def ledger(out):
    return [json.loads(l) for l in open(os.path.join(out, "ledger.jsonl"))]


def result(out, iid, arm):
    p = os.path.join(out, iid, arm, "result.json")
    return json.load(open(p)) if os.path.exists(p) else None


def test_order_is_literal_hash_order():
    ids = ["django__django-15561", "sympy__sympy-13091", "psf__requests-6028"]
    dig = lambda i: hashlib.sha256(("D1-BRIDGE-ORDER-v1\n" + i).encode()).hexdigest()
    assert BA.task_order(ids) == sorted(ids, key=dig)
    for i in ids:
        perms = list(itertools.permutations(P.ARMS))
        assert BA.arm_order(i, ARMS) == list(perms[int(dig(i), 16) % 24])
        assert sorted(BA.arm_order(i, ["silent", "number", "bats"])) == ["bats", "number", "silent"]
    sch = BA.schedule(ids, ARMS)
    assert [s[0] for s in sch] == [i for i in BA.task_order(ids) for _ in ARMS]      # arms of a task are adjacent


def test_order_mixes_arms_on_the_real_selection():
    sel = json.load(open(SEL)); ids = BA.selection_ids(sel, False)
    assert len(ids) == 100 and len(BA.selection_ids(sel, True)) == 5
    firsts = {BA.arm_order(i, ARMS)[0] for i in ids}
    assert firsts == set(ARMS)                                                         # no arm is always first


def test_full_run_interleaved_and_resume_skips():
    ids = ["a__a-1", "b__b-2"]
    b, run, out = mk(ids)
    assert b.run() == "END"
    starts = [(e["id"], e["arm"]) for e in ledger(out) if e["event"] == "run_start"]
    assert starts == BA.schedule(ids, ARMS) and len(run.calls) == 8
    ends = [e for e in ledger(out) if e["event"] == "run_end"]
    assert all(e["model_versions"] == ["gemini-3-flash-preview"] and e["code"] == "SUB_NONEMPTY" for e in ends)
    assert all(result(out, i, a)["S"] == 1 for i in ids for a in ARMS)
    b2, run2, _ = mk(ids, out=out)
    assert b2.run() == "END" and run2.calls == []
    assert sum(1 for e in ledger(out) if e["event"] == "skip_done") == 8


def test_eng_fail_rerun_once_then_success():
    ids = ["a__a-1"]
    b, run, out = mk(ids, {("a__a-1", "bats"): [{"exit_status": None}]})
    b.run()
    r = result(out, "a__a-1", "bats")
    assert r["code"] == "SUB_NONEMPTY" and len(r["attempt_history"]) == 1 and r["attempt_history"][0]["code"] == "ENG_FAIL"
    assert sum(1 for c in run.calls if c[1] == "bats") == 2
    assert os.path.exists(r["attempt_history"][0]["moved_to"])
    assert any(e["event"] == "rerun" for e in ledger(out))


def test_eng_fail_twice_stays_unknown_no_third_run():
    b, run, out = mk(["a__a-1"], {("a__a-1", "number"): ["crash", "raise", {}]})
    b.run()
    r = result(out, "a__a-1", "number")
    assert r["code"] == "ENG_FAIL" and (r["S"], r["H"], r["W"]) == (None, None, None)
    assert len(r["attempt_history"]) == 1 and sum(1 for c in run.calls if c[1] == "number") == 2
    b2, run2, _ = mk(["a__a-1"], out=out)                       # resume must not rerun a finished ENG_FAIL
    b2.run(); assert run2.calls == []


def test_interrupted_slot_counts_as_an_attempt():
    ids = ["a__a-1"]
    b, run, out = mk(ids)
    os.makedirs(os.path.join(out, "a__a-1", "silent"))           # crashed earlier: directory without result
    b.run()
    r = result(out, "a__a-1", "silent")
    assert r["code"] == "SUB_NONEMPTY" and r["attempt_history"][0]["reason"] == "incomplete"
    # an interrupted rerun that also left nothing => terminal ENG_FAIL, no third attempt
    hist = os.path.join(out, "attempts", "a__a-1.silent.history.json")
    json.dump([{"moved_to": "x"}, {"moved_to": "y"}], open(hist, "w"))
    os.remove(os.path.join(out, "a__a-1", "silent", "result.json"))
    os.remove(os.path.join(out, "a__a-1", "silent", "record.json"))          # the interrupted rerun left nothing
    b2, run2, _ = mk(ids, out=out); b2.run()
    r = result(out, "a__a-1", "silent")
    assert r["code"] == "ENG_FAIL" and "TWO_ATTEMPTS_NO_RESULT" in r["why"] and not [c for c in run2.calls if c[1] == "silent"]


def test_identity_mismatch_stops_and_keeps_results():
    # review C2-1: a finished result whose identity no longer matches is NEVER moved or rerun; the batch stops
    ids = ["a__a-1"]
    b, run, out = mk(ids); b.run()
    before = {a: json.load(open(os.path.join(out, "a__a-1", a, "result.json"))) for a in ARMS}
    b2, run2, _ = mk(ids, out=out); b2.bridge_code = "f" * 64
    assert b2.run() == "STOPPED" and run2.calls == []
    stop = ledger(out)[-2]
    assert stop["event"] == "STOP_IDENTITY_MISMATCH" and stop["scope"] == "directory_scan" and stop["n"] > 0
    assert {a: json.load(open(os.path.join(out, "a__a-1", a, "result.json"))) for a in ARMS} == before
    assert not os.listdir(os.path.join(out, "attempts"))


def test_spend_stop_line_before_and_during():
    ids = ["a__a-1", "b__b-2"]
    first = BA.schedule(ids, ARMS)[0]
    b, run, out = mk(ids, {first: [dict(exit_status="Submitted", submission="diff FIX", F=b"diff FIX", usd=5.0)]}, stop=5.0)
    assert b.run() == "STOPPED" and len(run.calls) == 1
    assert any(e["event"] == "STOP_SPEND" for e in ledger(out))
    assert sum(1 for i in ids for a in ARMS if result(out, i, a) is None) == 7      # unfinished slots: no record => unknown


def test_resource_stop_inside_trajectory_is_not_rerun_and_stops_batch():
    ids = ["a__a-1", "b__b-2"]
    first = BA.schedule(ids, ARMS)[0]
    b, run, out = mk(ids, {first: [dict(exit_status="SpendStop", F=b"diff FIX", usd=10.0)]}, stop=10.0)
    assert b.run() == "STOPPED" and len(run.calls) == 1
    r = result(out, *first)
    assert r["code"] == "ENG_FAIL" and r["resource_stop"] and "attempt_history" not in r


def test_image_not_ready_skips_without_consuming_attempts():
    b, run, out = mk(["a__a-1", "b__b-2"], not_ready=("a__a-1",))
    assert b.run() == "END_INCOMPLETE"                                              # review m9
    assert ledger(out)[-1]["event"] == "batch_end" and ledger(out)[-1]["n_no_result"] == 4
    assert all(c[0] == "b__b-2" for c in run.calls) and result(out, "a__a-1", "bats") is None
    assert sum(1 for e in ledger(out) if e["event"] == "IMAGE_NOT_READY") == 4
    assert not os.path.exists(os.path.join(out, "attempts", "a__a-1.bats.history.json"))


def test_week_cap_stops():
    b, run, out = mk(["a__a-1"])
    b.week_exhausted = lambda: ["eval_launches"]
    assert b.run() == "STOPPED" and run.calls == []


def test_eng_fail_streak_pauses_batch():
    # systematic failure (e.g. a retired model id): every attempt crashes; after 3 consecutive ENG_FAIL attempts the
    # batch pauses instead of burning one rerun per slot across all 400 slots
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    assert b.run() == "STOPPED" and len(run.calls) == BA.ENG_STREAK_STOP
    assert ledger(out)[-2]["event"] == "STOP_ENG_STREAK"


def test_streak_attempts_can_be_voided_after_fix():
    # review item 2: a systematic failure must not permanently cost the slots inside the streak
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    assert b.run() == "STOPPED"
    stop = ledger(out)[-2]; assert stop["event"] == "STOP_ENG_STREAK"
    BA.void_streak(out, stop["streak_file"], "API key expired; replaced")
    b2, run2, _ = mk(ids, out=out)
    assert b2.run() == "END"
    rs = [result(out, i, a) for i in ids for a in ARMS]
    assert all(r and r["code"] == "SUB_NONEMPTY" for r in rs)                      # no slot lost to the streak
    assert any(e["event"] == "VOID_STREAK" for e in ledger(out))


def test_eval_exception_resumes_evaluation_without_rerunning_agent():
    ids = ["a__a-1"]
    b, run, out = mk(ids)
    first = BA.schedule(ids, ARMS)[0]
    good = b.eval_fn
    b.eval_fn = lambda tdir, **kw: (_ for _ in ()).throw(RuntimeError("docker daemon down"))
    assert b.run() == "STOPPED" and len(run.calls) == 1
    b.eval_fn = good
    assert b.run() == "END" and len(run.calls) == 4                                # first slot evaluated only
    assert result(out, *first)["code"] == "SUB_NONEMPTY" and any(e["event"] == "eval_only_resume" for e in ledger(out))


def test_crash_between_history_and_move_counts_once():
    ids = ["a__a-1"]
    b, run, out = mk(ids)
    tdir = os.path.join(out, "a__a-1", "silent"); os.makedirs(tdir)
    hist = os.path.join(out, "attempts", "a__a-1.silent.history.json")
    json.dump([{"moved_to": os.path.join(out, "attempts", "x"), "reason": "eng_fail_rerun", "pending": True}], open(hist, "w"))
    b.run()
    h = json.load(open(hist))
    assert len(h) == 1 and "pending" not in h[0] and os.path.exists(os.path.join(out, "attempts", "x"))
    assert result(out, "a__a-1", "silent")["code"] == "SUB_NONEMPTY"


def test_streak_resets_on_success():
    ids = ["a__a-1", "b__b-2"]
    sch = BA.schedule(ids, ARMS)
    plan = {sch[0]: ["crash", "crash"], sch[2]: ["crash"]}
    b, run, out = mk(ids, plan)
    assert b.run() == "END" and len(run.calls) == 8 + 2          # slot 0 used both attempts, slot 2 one rerun


def test_pricing_pause_blocks_before_any_slot():
    b, run, out = mk(["a__a-1"])
    b.spend.block("unpriced response")
    assert b.run() == "STOPPED" and run.calls == [] and ledger(out)[-2]["event"] == "STOP_PRICING"


def test_dev_and_formal_ledgers_are_separate():
    d = FK.tmpdir(); p = os.path.join(d, "spend.json")
    Spend(p, 15.0, dev=True)
    try:
        Spend(p, 15.0, dev=False)
    except SystemExit:
        pass
    else:
        raise AssertionError("formal batch accepted the dry-run ledger")
    sp = Spend(p, 15.0, dev=True); sp.add(1.0, provisional=True); sp.add(2.0)
    assert abs(sp.total() - 3.0) < 1e-12 and not sp.exhausted()


def _calls_dir(out, rel, attempt, calls):
    d = os.path.join(out, rel); os.makedirs(d, exist_ok=True)
    json.dump({"attempt": attempt}, open(os.path.join(d, "attempt.json"), "w"))
    json.dump(calls, open(os.path.join(d, "calls.json"), "w"))
    return d


def test_reconcile_consistent_and_each_discrepancy():
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 100.0, key_prefix="a1")
    calls = [{"i": 1, "response_id": "r1", "cost": 0.01}, {"i": 2, "response_id": "r2", "cost": 0.02}]
    _calls_dir(out, "x__y-1/silent", "a1", calls)
    for c in calls:
        sp.add(c["cost"], key=sp.call_key(c))
    assert reconcile(out, sp.path) == []
    sp.add(0.02, key="a1:2:r2")                                                      # double charge
    assert any(p.startswith("DUP_CHARGE") for p in reconcile(out, sp.path))
    out2 = FK.tmpdir(); sp2 = Spend(os.path.join(out2, "spend.json"), 100.0, key_prefix="b1")
    _calls_dir(out2, "attempts/x__y-1.bats-1-1", "b1", [{"i": 1, "response_id": "q1", "cost": 0.03},
                                                        {"i": 2, "response_id": "q2", "cost": None, "pricing_status": None}])
    sp2.add(0.031, key="b1:1:q1")                                                    # wrong amount
    sp2.add(0.5, key="zz:9:nope")                                                    # charge without a call
    p2 = reconcile(out2, sp2.path)
    assert any(p.startswith("COST_MISMATCH b1:1:q1") for p in p2) and any(p.startswith("ORPHAN_CHARGE zz:9:nope") for p in p2)
    assert any(p.startswith("MISSING_CHARGE b1:2:q2") for p in p2)                   # crash after the response, before the charge


def test_reconcile_open_provisional_and_totals():
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 100.0, key_prefix="c1")
    _calls_dir(out, "x__y-1/number", "c1", [{"i": 1, "response_id": "s1", "cost": None, "pricing_status": "UNREGISTERED_VERSION"}])
    sp.add(1.0, provisional=True, key="c1:1:s1")
    assert reconcile(out, sp.path) == ["OPEN_PROVISIONAL 1 reservation(s) not reconciled"]
    sp.clear("bill checked", reconciled_usd=0.004, bill_id="bill-1")
    assert reconcile(out, sp.path) == []
    st = json.load(open(sp.path)); st["usd"] += 1.0; json.dump(st, open(sp.path, "w"))  # tampered totals
    assert any(p.startswith("TOTALS_MISMATCH") for p in reconcile(out, sp.path))


def test_batch_stops_on_reconcile_problem_before_spending():
    ids = ["a__a-1"]
    b, run, out = mk(ids)
    _calls_dir(out, "attempts/a__a-1.silent-1-1", "old1", [{"i": 1, "response_id": "r", "cost": 0.01}])   # never charged
    assert b.run() == "STOPPED" and run.calls == []
    assert ledger(out)[-2]["event"] == "STOP_RECONCILE"
    b.spend.clear("charge found on the bill", reconciled_usd=0.01, resolves=["old1:1:r"], bill_id="bill-2")
    assert b.run() == "END" and len(run.calls) == 4


def test_streak_pause_persists_across_processes_until_resolved():
    # review C2-2: a new Batch (new process) must not lift the pause by itself
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    assert b.run() == "STOPPED" and len(run.calls) == BA.ENG_STREAK_STOP
    b2, run2, _ = mk(ids, out=out, default="crash")
    assert b2.run() == "STOPPED" and run2.calls == [] and ledger(out)[-2]["event"] == "STOP_ENG_STREAK_UNRESOLVED"
    BA.ack_streak(out, "transient outage, attempts keep counting")
    b3, run3, _ = mk(ids, out=out)
    assert b3.run() == "END"
    used = {(c[0], c[1]) for c in run.calls}
    for iid, arm in used:                                   # acked (not voided) attempts still count
        r = result(out, iid, arm)
        assert r["code"] in ("SUB_NONEMPTY", "ENG_FAIL")


def test_reconcile_before_immediate_rerun():
    # review C2-3: attempt 1 leaves a call in calls.json without a charge (accounting broke) and fails;
    # the immediate rerun must NOT start before the ledger is reconciled
    ids = ["a__a-1"]
    first = BA.schedule(ids, ARMS)[0]
    b, run, out = mk(ids)
    orig = run.__call__
    def run_fn(*, iid, arm, attempt, tdir, image_id):
        if (iid, arm) == first and not [c for c in run.calls if c[:2] == first]:
            run.calls.append((iid, arm, attempt))
            json.dump([{"i": 1, "response_id": "r1", "cost": 0.01}], open(os.path.join(tdir, "calls.json"), "w"))
            return 1                                         # crash: no record => ENG_FAIL
        return orig(iid=iid, arm=arm, attempt=attempt, tdir=tdir, image_id=image_id)
    b.run_fn = run_fn
    assert b.run() == "STOPPED"
    assert len([c for c in run.calls if c[:2] == first]) == 1
    stops = [e for e in ledger(out) if e["event"] == "STOP_RECONCILE"]
    assert stops and stops[0]["where"].startswith("before") and "MISSING_CHARGE" in stops[0]["problems"][0]


def test_void_interrupted_before_move_is_completed_on_resume():
    # review C2-4: crash after the void marks were written but before the terminal dir was moved
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    assert b.run() == "STOPPED"
    stop = [e for e in ledger(out) if e["event"] == "STOP_ENG_STREAK"][-1]
    streak = json.load(open(stop["streak_file"]))["attempts"]
    for e in streak:                                         # simulate: marks persisted, move never happened
        hp = os.path.join(out, "attempts", f'{e["id"]}.{e["arm"]}.history.json')
        h = json.load(open(hp)) if os.path.exists(hp) else []
        for x in h:
            if x.get("attempt") == e["attempt"]:
                x["voided"] = True
        if e["attempt"] not in BA.voided_attempts(h):
            h.append({"attempt": e["attempt"], "voided": True, "reason": "voided_streak_mark"})
        json.dump(h, open(hp, "w"))
    ss = BA.load_streak(out); ss.update(streak=[], tripped=None); BA.save_streak(out, ss)
    b2, run2, _ = mk(ids, out=out)
    assert b2.run() == "END"
    assert all(result(out, i, a)["code"] == "SUB_NONEMPTY" for i in ids for a in ARMS)


def test_clear_is_idempotent_per_bill():
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 100.0, key_prefix="c1")
    _calls_dir(out, "x__y-1/number", "c1", [{"i": 1, "response_id": "s1", "cost": None, "pricing_status": "UNREGISTERED_VERSION"}])
    sp.add(1.0, provisional=True, key="c1:1:s1")
    assert sp.clear("bill checked", reconciled_usd=0.01, bill_id="B-1") is True
    assert sp.clear("bill checked (retry)", reconciled_usd=0.01, bill_id="B-1") is False
    st = json.load(open(sp.path))
    assert abs(st["usd"] - 0.01) < 1e-12 and reconcile(out, sp.path) == []
    try:
        sp.clear("no id", reconciled_usd=0.01)
    except ValueError:
        pass
    else:
        raise AssertionError("clear without bill_id accepted")



# ---------------- addendum 2: failure-path review (2026-10-20) ----------------
class InfraJudge(FK.FakeJudge):
    """Every non-empty patch: infrastructure-unknown verdict (evaluation environment broken)."""
    def __call__(self, instance_id, patch, tag, workdir, *, img, expected_id, week=None):
        if len(patch) == 0:
            return {"verdict": 0, "reason": None, "slots": ["empty"], "empty": True}
        self.calls.append({"tag": tag})
        return {"verdict": None, "reason": "INFRA_UNRECOVERED", "slots": []}


def test_judge_infra_streak_pauses_and_reeval_once():
    # review I1: 3 consecutive infrastructure-unknown products pause the batch; after the fix the author lifts the
    # pause and re-evaluates ONCE from the saved artifacts (no agent rerun), then the batch continues
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, judge=InfraJudge())
    assert b.run() == "STOPPED" and len(run.calls) == BA.JUDGE_STREAK_STOP
    assert any(e["event"] == "STOP_JUDGE_STREAK" for e in ledger(out))
    b2, run2, _ = mk(ids, out=out)                              # judge fixed, but the pause persists until acked
    assert b2.run() == "STOPPED" and run2.calls == [] and ledger(out)[-2]["event"] == "STOP_JUDGE_STREAK_UNRESOLVED"
    BA.ack_judge_streak(out, "swebench env repaired")
    done = b2.reeval_infra("swebench env repaired")
    assert len(done) == BA.JUDGE_STREAK_STOP and run2.calls == []
    for iid, arm in done:
        r = result(out, iid, arm)
        assert (r["S"], r["reeval_count"]) == (1, 1) and os.path.exists(r["reeval_of"])
    assert b2.reeval_infra("again") == []                       # at most once
    assert b2.run() == "END"


def test_directory_scan_stops_before_spending():
    # review I2 (a): the first task's slots have no result yet; a changed identity must stop BEFORE running them
    ids = ["a__a-1", "b__b-2"]
    first = BA.task_order(ids)[0]
    b, run, out = mk(ids, not_ready=(first,))
    assert b.run() == "END_INCOMPLETE"
    b2, run2, _ = mk(ids, out=out, bridge_code="f" * 64)
    assert b2.run() == "STOPPED" and run2.calls == []
    assert ledger(out)[-2]["event"] == "STOP_IDENTITY_MISMATCH"
    assert not os.path.exists(os.path.join(out, first, "silent", "result.json"))


def test_prior_dir_slots_are_skipped_under_new_identity():
    # addendum 2 (I2 rule): after a pre-registered code change, a NEW output dir continues the experiment; slots
    # finished in the prior dir (under its own identity) are skipped, unfinished ones run in the new dir
    ids = ["a__a-1", "b__b-2"]
    first = BA.task_order(ids)[0]
    b, run, old = mk(ids, not_ready=(first,))
    assert b.run() == "END_INCOMPLETE"
    b2, run2, new = mk(ids, prior=[(old, b.bridge_code)], bridge_code="e" * 64)
    assert b2.run() == "END" and {c[0] for c in run2.calls} == {first} and len(run2.calls) == 4
    assert sum(1 for e in ledger(new) if e["event"] == "skip_prior") == 4
    b3, run3, _ = mk(ids, prior=[(old, "0" * 64)], bridge_code="e" * 64)     # wrong identity declared for the prior dir
    assert b3.run() == "STOPPED" and run3.calls == []


def test_output_dir_lock_excludes_second_batch():
    # review I4: a second batch on the same output dir must not run while the first holds the lock
    import threading
    ids = ["a__a-1"]
    b1, run1, out = mk(ids)
    gate, entered = threading.Event(), threading.Event()
    orig = run1.__call__
    def slow(**kw):
        entered.set(); gate.wait(10)
        return orig(**kw)
    b1.run_fn = slow
    t = threading.Thread(target=b1.run); t.start()
    assert entered.wait(10)
    b2, run2, _ = mk(ids, out=out)
    try:
        b2.run()
    except SystemExit as e:
        assert "batch.lock" in str(e)
    else:
        raise AssertionError("second batch ran on a locked output dir")
    gate.set(); t.join(10)
    assert run2.calls == []


def test_run_subprocess_timeout_kills_group():
    # review m1 + I4: wall-clock limit; the whole process group is killed
    import time as _t
    log = os.path.join(FK.tmpdir(), "x.log"); t0 = _t.time()
    rc = BA.run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"], log, 1)
    assert rc == "TIMEOUT" and _t.time() - t0 < 15 and BA.CURRENT["proc"] is None
    assert BA.run_subprocess([sys.executable, "-c", "print('ok')"], log, 30) == 0


def test_clear_crash_between_journal_and_totals_is_repaired():
    # review m4: the journal line was written but the totals replacement failed; retrying the same bill_id repairs
    # the totals without a second journal line
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 100.0, key_prefix="c1")
    _calls_dir(out, "x__y-1/number", "c1", [{"i": 1, "response_id": "s1", "cost": None, "pricing_status": "UNREGISTERED_VERSION"}])
    sp.add(1.0, provisional=True, key="c1:1:s1")
    import spend as SPM
    real = SPM.os.replace; n = {"k": 0}
    def flaky(a, b):
        n["k"] += 1
        if n["k"] == 1:
            raise OSError("disk full")
        return real(a, b)
    SPM.os.replace = flaky
    try:
        try:
            sp.clear("bill", reconciled_usd=0.01, bill_id="B-9")
        except OSError:
            pass
    finally:
        SPM.os.replace = real
    assert any(p.startswith("TOTALS_MISMATCH") for p in reconcile(out, sp.path))
    assert sp.clear("bill (retry)", reconciled_usd=0.01, bill_id="B-9") == "REPAIRED"
    assert reconcile(out, sp.path) == []
    st = json.load(open(sp.path)); st["usd"] += 5; json.dump(st, open(sp.path, "w"))     # any other drift: rebuild
    sp.rebuild_totals("rebuilt from journal after manual check")
    assert reconcile(out, sp.path) == []


def test_void_reentry_and_wrong_streak_file():
    # review m5: (a) a void interrupted at the move leaves no dangling pending entry after re-running it;
    # (b) a streak file other than the one the current pause refers to is refused
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    assert b.run() == "STOPPED"
    tripped = BA.load_streak(out)["tripped"]
    other = os.path.join(out, "streak_old.json"); json.dump({"attempts": []}, open(other, "w"))
    try:
        BA.void_streak(out, other, "wrong file")
    except SystemExit:
        pass
    else:
        raise AssertionError("void accepted a streak file that is not the current pause")
    real = BA.shutil.move; n = {"k": 0}
    def flaky(a, c):
        n["k"] += 1
        if n["k"] == 1:
            raise OSError("interrupted")
        return real(a, c)
    BA.shutil.move = flaky
    try:
        try:
            BA.void_streak(out, tripped, "API key replaced")
        except OSError:
            pass
    finally:
        BA.shutil.move = real
    BA.void_streak(out, tripped, "API key replaced (re-run)")
    for hp in glob.glob(os.path.join(out, "attempts", "*.history.json")):
        assert not any(h.get("pending") for h in json.load(open(hp))), hp
    b2, run2, _ = mk(ids, out=out)
    assert b2.run() == "END"


def test_streak_counts_eval_written_before_crash():
    # review m6: the evaluation wrote an ENG_FAIL result, then the process died before the streak was saved;
    # on resume that attempt still counts, so the pause comes after 3 failures, not 4
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    b, run, out = mk(ids, default="crash")
    real_eval = b.eval_fn
    def dying_eval(tdir, **kw):
        r = real_eval(tdir, **kw)
        raise KeyboardInterrupt("process killed after the result was written")
    b.eval_fn = dying_eval
    try:
        b.run()
    except KeyboardInterrupt:
        pass
    b2, run2, _ = mk(ids, out=out, default="crash")
    assert b2.run() == "STOPPED" and len(run2.calls) == 2
    assert any(e["event"] == "STOP_ENG_STREAK" for e in ledger(out))


def test_disk_guard_stops_before_launch():
    b, run, out = mk(["a__a-1"], disk_free=lambda: 1e9)                                # review m8
    assert b.run() == "STOPPED" and run.calls == [] and ledger(out)[-2]["event"] == "STOP_DISK"


def test_formal_main_refuses_unsealed_inputs():
    # review m7: in a formal run, inputs whose sha256 differs from the expected (sealed) values stop main()
    d = FK.tmpdir()
    for name, obj in (("sel.json", {"high_60": [], "low_40": [], "dev_5": []}), ("meta.json", {}), ("img.json", {"images": {}})):
        json.dump(obj, open(os.path.join(d, name), "w"))
    argv = ["batch.py", "--selection", os.path.join(d, "sel.json"), "--tasks-meta", os.path.join(d, "meta.json"),
            "--images", os.path.join(d, "img.json"), "--out", os.path.join(d, "out"), "--spend", os.path.join(d, "sp.json"),
            "--week-state", os.path.join(d, "w.json"), "--agent-python", sys.executable, "--stop-usd", "200",
            "--expect-selection-sha", BA.sha256_file(os.path.join(d, "sel.json")),
            "--expect-tasks-meta-sha", "0" * 64, "--expect-images-sha", BA.sha256_file(os.path.join(d, "img.json"))]
    old = sys.argv; sys.argv = argv
    try:
        BA.main()
    except SystemExit as e:
        assert "tasks_meta sha256" in str(e), e
    else:
        raise AssertionError("main() accepted a tasks-meta file that differs from the expected sha256")
    finally:
        sys.argv = old


def test_delete_tool_refuses_when_docker_ps_fails():
    sys.path.insert(0, os.path.join(FK.ID.HERE, "tools"))
    import delete_images as DI
    class R:
        def __init__(self, rc, out): self.returncode, self.stdout = rc, out
    assert DI.containers_running(lambda *a, **k: R(1, ""))                         # review m10
    assert not DI.containers_running(lambda *a, **k: R(0, ""))



# ---------------- panoramic review (Codex, 2026-10-20) ----------------
class SplitJudge(FK.FakeJudge):
    """Before the repair: products containing b'INFRA' get an infrastructure-unknown verdict; others as FakeJudge."""
    def __call__(self, instance_id, patch, tag, workdir, *, img, expected_id, week=None):
        if b"INFRA" in patch:
            self.calls.append({"tag": tag, "patch": patch})
            return {"verdict": None, "reason": "INFRA_UNRECOVERED", "slots": []}
        return super().__call__(instance_id, patch, tag, workdir, img=img, expected_id=expected_id, week=week)


class ConstJudge(FK.FakeJudge):
    """After the repair: every launched product gets the same verdict (to expose any verdict that is re-judged)."""
    def __init__(self, v, stop_after=None):
        super().__init__(); self.v, self.stop_after = v, stop_after

    def __call__(self, instance_id, patch, tag, workdir, *, img, expected_id, week=None):
        if len(patch) == 0:
            return {"verdict": 0, "reason": None, "slots": ["empty"], "empty": True}
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            raise SystemExit("killed during re-evaluation")          # not an Exception: escapes evaluate's handler
        self.calls.append({"tag": tag, "patch": patch})
        return {"verdict": self.v, "reason": None, "slots": []}


def _reeval_setup(plan_specs):
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    plan = {(ids[0], arm): [spec] for arm, spec in zip(order, plan_specs)}
    b, run, out = mk(ids, plan=plan, judge=SplitJudge())
    assert b.run() == "END"
    return ids, order, out


def test_reeval_rejudges_only_infra_products():
    # panoramic ①: the submission's EVAL_UNSTABLE (or definite) verdict is never re-judged; only the INFRA product is
    ids, order, out = _reeval_setup([
        {"exit_status": "Submitted", "submission": "diff FLAKY", "F": b"diff INFRA FIX"},     # S/H unknown (unstable)
        {"exit_status": "Submitted", "submission": "diff FIX", "F": b"diff INFRA"}])          # S/H = 1 definite
    a0, a1 = order[0], order[1]
    assert (result(out, ids[0], a0)["S"], result(out, ids[0], a0)["W"]) == (None, None)
    assert (result(out, ids[0], a1)["S"], result(out, ids[0], a1)["W"]) == (1, None)
    j = ConstJudge(0)
    b2, run2, _ = mk(ids, out=out, judge=j)
    assert sorted(map(tuple, b2.reeval_infra("judge env repaired"))) == sorted([(ids[0], a0), (ids[0], a1)])
    r0, r1 = result(out, ids[0], a0), result(out, ids[0], a1)
    assert (r0["S"], r0["H"], r0["W"]) == (None, None, 0) and r0["verdicts"]["submission"]["reason"] == "EVAL_UNSTABLE"
    assert (r1["S"], r1["H"], r1["W"]) == (1, 1, 0)
    assert len(j.calls) == 2 and all(b"INFRA" in c["patch"] for c in j.calls) and run2.calls == []
    assert all(c["tag"].endswith("-re1") for c in j.calls)


def test_reeval_refused_under_another_identity():
    # panoramic ③: new code on an old dir => refused before anything is written; the old result stays as it was
    ids, order, out = _reeval_setup([{"exit_status": "Submitted", "submission": "diff FIX", "F": b"diff INFRA"}])
    p = os.path.join(out, ids[0], order[0], "result.json"); before = open(p, "rb").read()
    b2, _, _ = mk(ids, out=out, judge=ConstJudge(0), bridge_code="f" * 64)
    try:
        b2.reeval_infra("judge env repaired")
    except SystemExit as e:
        assert "refused" in str(e)
    else:
        raise AssertionError("re-evaluation under another identity was accepted")
    assert open(p, "rb").read() == before and ledger(out)[-1]["event"] == "REEVAL_REFUSED"


def test_reeval_interrupted_resumes_without_rejudging():
    # panoramic ⑤ + closure A: killed while the 2nd product was being judged. The resume keeps the 1st re-judged verdict
    # (1) even though the judge now says 0; the 2nd product's one re-judgment was claimed, so it is NOT judged again and
    # stays unknown (REEVAL_INTERRUPTED). result.json is written once, with reeval_count = 1
    ids, order, out = _reeval_setup([{"exit_status": "Submitted", "submission": "diff INFRA FIX", "F": b"diff INFRA2"}])
    p = os.path.join(out, ids[0], order[0], "result.json"); before = open(p, "rb").read()
    b2, _, _ = mk(ids, out=out, judge=ConstJudge(1, stop_after=1))
    try:
        b2.reeval_infra("judge env repaired")
    except SystemExit:
        pass
    assert open(p, "rb").read() == before                          # nothing half-written
    j = ConstJudge(0)
    b3, _, _ = mk(ids, out=out, judge=j)
    assert b3.reeval_infra("judge env repaired") == [[ids[0], order[0]]]
    r = result(out, ids[0], order[0])
    assert (r["S"], r["H"], r["W"], r["reeval_count"]) == (1, 1, None, 1) and j.calls == []
    assert len(r["reeval_products"]) == 1 and len(r["reeval_lost"]) == 1
    assert r["verdicts"]["F"]["reason"] == "REEVAL_INTERRUPTED"
    assert b3.reeval_infra("again") == []


def test_reeval_verdict_lost_before_marker_write_is_not_rejudged():
    # closure review A: the judge returned (W = 1) but storing the verdict in the marker failed; the resume must not
    # judge that product a second time (which here would give 0): it stays unknown
    ids, order, out = _reeval_setup([{"exit_status": "Submitted", "submission": "diff FIX", "F": b"diff INFRA"}])
    orig = BA.ID.atomic_json
    def failing(path, obj):
        if path.endswith(".reeval1.json") and obj.get("done"):
            raise OSError("disk full")
        return orig(path, obj)
    BA.ID.atomic_json = failing
    try:
        b2, _, _ = mk(ids, out=out, judge=ConstJudge(1))
        assert b2.reeval_infra("judge env repaired") == []
    finally:
        BA.ID.atomic_json = orig
    assert any(e["event"] == "REEVAL_EXCEPTION" for e in ledger(out))
    j = ConstJudge(0)
    b3, _, _ = mk(ids, out=out, judge=j)
    assert b3.reeval_infra("judge env repaired") == [[ids[0], order[0]]]
    r = result(out, ids[0], order[0])
    assert j.calls == [] and (r["S"], r["H"], r["W"]) == (1, 1, None) and len(r["reeval_lost"]) == 1


def test_new_dir_inherits_unresolved_streak():
    # closure review B: the prior dir ended with two consecutive ENG_FAILs (a terminal slot, not rerun-eligible); the
    # continuation dir must carry them over, so the next failure is the 3rd consecutive one and pauses at once
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    free = iter([1e12, 1e12] + [0] * 50)
    b, run, old = mk(ids, plan={(ids[0], order[0]): ["crash", "crash"]}, disk_free=lambda: next(free))
    assert b.run() == "STOPPED" and len(BA.load_streak(old)["streak"]) == 2
    b2, run2, new = mk(ids, plan={(ids[0], order[1]): ["crash", "crash"]}, prior=[(old, b.bridge_code)],
                       bridge_code="e" * 64)
    assert b2.run() == "STOPPED" and len(run2.calls) == 1
    ev = [e["event"] for e in ledger(new)]
    assert "STREAK_INHERITED" in ev and "STOP_ENG_STREAK" in ev and "skip_prior" in ev
    st = BA.load_streak(new)
    assert len(st["streak"]) == 3 and st["tripped"] and st["inherited_from"]["dir"] == os.path.abspath(old)
    b3, run3, _ = mk(ids, prior=[(old, b.bridge_code)], bridge_code="e" * 64, out=new)   # no re-inheritance
    assert b3.run() == "STOPPED" and run3.calls == [] and ledger(new)[-2]["event"] == "STOP_ENG_STREAK_UNRESOLVED"
    # AB check 1: the void acts where each attempt lives: the prior dir's terminal ENG_FAIL is moved aside THERE, so
    # the slot is no longer "done" in the prior dir and runs again in the new dir (no silently kept voided failure)
    voided = BA.void_streak(new, st["tripped"], "cause found and fixed")
    assert sorted({v[3] for v in voided}) == sorted({os.path.abspath(old), os.path.abspath(new)})
    assert result(old, ids[0], order[0]) is None and not b.__class__._prior_done(
        mk(ids, prior=[(old, b.bridge_code)], bridge_code="e" * 64, out=new)[0], ids[0], order[0])
    b4, run4, _ = mk(ids, prior=[(old, b.bridge_code)], bridge_code="e" * 64, out=new)
    assert b4.run() == "END" and sorted(c[1] for c in run4.calls) == sorted(ARMS)    # order[0] and order[1] again + 2
    assert all(result(new, ids[0], a)["code"] == "SUB_NONEMPTY" for a in ARMS)


def test_state_chain_skips_empty_dir_and_refuses_lost_state():
    # AB check 2: old dir A ends with 2 consecutive failures; dir B was started (prior A) but crashed before saving any
    # state and holds nothing; dir C declares A and B: it must inherit A's streak (B is skipped), so the next failure
    # pauses at once. If B held results but no state, C must refuse to start instead of assuming an empty streak.
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    free = iter([1e12, 1e12] + [0] * 50)
    a, _, A = mk(ids, plan={(ids[0], order[0]): ["crash", "crash"]}, disk_free=lambda: next(free))
    assert a.run() == "STOPPED"
    B = FK.tmpdir()                                               # started, crashed before any state was written
    c, runc, C = mk(ids, plan={(ids[0], order[1]): ["crash", "crash"]}, prior=[(A, a.bridge_code), (B, "e" * 64)],
                    bridge_code="f" * 64)
    assert c.run() == "STOPPED" and len(runc.calls) == 1
    assert BA.load_streak(C)["inherited_from"]["dir"] == os.path.abspath(A)
    B2 = FK.tmpdir(); FK.write_record(os.path.join(B2, ids[0], order[1]), iid=ids[0], arm=order[1], attempt="zz",
                                      exit_status=None, bridge_code="e" * 64)
    d, rund, D = mk(ids, prior=[(A, a.bridge_code), (B2, "e" * 64)], bridge_code="f" * 64)
    assert d.run() == "STOPPED" and rund.calls == [] and any(e["event"] == "STOP_STATE_CHAIN" for e in ledger(D))


def test_inheritance_applies_result_written_before_crash():
    # a prior dir crashed between writing a result and applying it (panoramic ⑥ window) and was then abandoned for a
    # new dir: the new dir applies that result when it inherits, so the failure still counts
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    a, _, A = mk(ids, plan={(ids[0], order[0]): ["crash", "crash"], (ids[0], order[1]): ["crash", "crash"]})
    orig, n = a._after, {"k": 0}
    def dying(*x, **k):
        n["k"] += 1
        if n["k"] == 2:
            raise SystemExit("power loss")
        return orig(*x, **k)
    a._after = dying
    try:
        a.run()
    except SystemExit:
        pass
    assert len(BA.load_streak(A)["streak"]) == 1
    c, runc, C = mk(ids, plan={(ids[0], order[1]): ["crash", "crash"]}, prior=[(A, a.bridge_code)], bridge_code="f" * 64)
    assert c.run() == "STOPPED" and len(runc.calls) == 1                       # 2 inherited + 1 new = 3 => pause
    assert [x[2] for x in [e for e in ledger(C) if e["event"] == "STREAK_INHERITED"][0]["applied_late"]] != []


def test_streak_update_completed_after_crash():
    # panoramic ⑥: the slot's 2nd ENG_FAIL result is written, then the process dies before the streak update; on
    # resume the update is applied once, so the 3rd consecutive ENG_FAIL pauses the batch at once
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    plan = {(ids[0], order[0]): ["crash", "crash"], (ids[0], order[1]): ["crash", "crash"]}
    b, run, out = mk(ids, plan=plan)
    orig, n = b._after, {"k": 0}
    def dying(*a, **k):
        n["k"] += 1
        if n["k"] == 2:
            raise SystemExit("power loss")
        return orig(*a, **k)
    b._after = dying
    try:
        b.run()
    except SystemExit:
        pass
    assert len(BA.load_streak(out)["streak"]) == 1
    b2, run2, _ = mk(ids, plan={(ids[0], order[1]): ["crash", "crash"]}, out=out)
    assert b2.run() == "STOPPED" and len(run2.calls) == 1
    assert any(e["event"] == "STOP_ENG_STREAK" for e in ledger(out))
    st = BA.load_streak(out)
    assert len(st["streak"]) == 3 and len(st["applied"]) == len(set(st["applied"])) == 3


def test_streak_success_after_crash_resets_once():
    # panoramic ⑥ (success side): a successful result written just before a crash still clears the old streak
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    plan = {(ids[0], order[0]): ["crash", "crash"]}
    b, run, out = mk(ids, plan=plan)
    orig, n = b._after, {"k": 0}
    def dying(*a, **k):
        n["k"] += 1
        if n["k"] == 3:                                            # the success of the 2nd slot
            raise SystemExit("power loss")
        return orig(*a, **k)
    b._after = dying
    try:
        b.run()
    except SystemExit:
        pass
    assert len(BA.load_streak(out)["streak"]) == 2
    # the next slot fails once: with the reset applied the streak is 1 (rerun, then success); without it, 3 => pause
    b2, run2, _ = mk(ids, plan={(ids[0], order[2]): ["crash"]}, out=out)
    assert b2.run() == "END" and BA.load_streak(out)["streak"] == []
    assert not any(e["event"] == "STOP_ENG_STREAK" for e in ledger(out))


def test_reconcile_covers_prior_dirs_sharing_the_ledger():
    # panoramic ④: an experiment continued in a new dir keeps its one spend ledger; the prior dir's charges are not
    # orphans, and the batch (with --prior-out) is not paused by them
    old, new, ld = FK.tmpdir(), FK.tmpdir(), FK.tmpdir()
    sp = Spend(os.path.join(ld, "spend.json"), 100.0, key_prefix="p1")
    calls = [{"i": 1, "response_id": "r1", "cost": 0.01}]
    _calls_dir(old, "a__a-1/silent", "p1", calls)
    for c in calls:
        sp.add(c["cost"], key=sp.call_key(c))
    assert any(x.startswith("ORPHAN_CHARGE") for x in reconcile(new, sp.path))
    assert reconcile([new, old], sp.path) == []
    b, _, _ = mk(["a__a-1"], out=new, prior=[(old, "e" * 64)])
    b.spend = sp
    assert b._reconciled("test") is True



# ---------------- targeted acceptance of the B fixes (2026-10-20) ----------------
def _attempt_cap_ok(dirs, ids):
    """Every slot in every dir: at most 2 counted (non-voided) agent attempts."""
    for d in dirs:
        for iid in ids:
            for arm in ARMS:
                hp = os.path.join(d, "attempts", f"{iid}.{arm}.history.json")
                hist = json.load(open(hp)) if os.path.exists(hp) else []
                r = result(d, iid, arm)
                n = len(BA.active(hist)) + (1 if r and BA.counts_as_attempt(r, hist) else 0)
                assert n <= 2, (d, iid, arm, n)


def _analysis_view(pairs, ids):
    import analyze as AN
    recs, source = AN.load_results_multi(pairs, {i: "hi" for i in ids}, ARMS)
    return recs, source


def test_acceptance_cross_dir_void_rerun_and_analysis():
    # (1) after a cross-dir void the prior dir's failed result is no longer used, the slot really runs again, and the
    # analysis (last dir wins) reads the new result for every slot; attempt cap holds in both dirs
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    free = iter([1e12, 1e12] + [0] * 50)
    b, _, old = mk(ids, plan={(ids[0], order[0]): ["crash", "crash"]}, disk_free=lambda: next(free))
    assert b.run() == "STOPPED"
    recs, _ = _analysis_view([(old, b.bridge_code)], ids)
    assert recs[(ids[0], order[0])]["code"] == "ENG_FAIL"                      # before: the failure would be used
    b2, run2, new = mk(ids, plan={(ids[0], order[1]): ["crash"]}, prior=[(old, b.bridge_code)], bridge_code="e" * 64)
    assert b2.run() == "STOPPED" and len(run2.calls) == 1                     # 3rd consecutive failure across dirs
    BA.void_streak(new, BA.load_streak(new)["tripped"], "cause found and fixed")
    b3, run3, _ = mk(ids, prior=[(old, b.bridge_code)], bridge_code="e" * 64, out=new)
    assert b3.run() == "END" and sorted(c[1] for c in run3.calls) == sorted(ARMS)
    recs, source = _analysis_view([(old, b.bridge_code), (new, "e" * 64)], ids)
    assert all(recs[(ids[0], a)]["code"] == "SUB_NONEMPTY" and recs[(ids[0], a)]["S"] == 1 for a in ARMS)
    assert all(source[f"{ids[0]}/{a}"] == os.path.abspath(new) for a in ARMS)
    _attempt_cap_ok([old, new], ids)


def test_acceptance_state_chain_pause_and_recovery():
    # (2) the last prior dir has no state: the earlier streak is not lost, the 3rd consecutive failure pauses, and
    # after the usual void the run completes; the analysis over the whole chain reads only valid results
    ids = ["a__a-1"]; order = BA.arm_order(ids[0], ARMS)
    free = iter([1e12, 1e12] + [0] * 50)
    a, _, A = mk(ids, plan={(ids[0], order[0]): ["crash", "crash"]}, disk_free=lambda: next(free))
    assert a.run() == "STOPPED"
    B = FK.tmpdir()
    c, runc, C = mk(ids, plan={(ids[0], order[1]): ["crash"]}, prior=[(A, a.bridge_code), (B, "e" * 64)],
                    bridge_code="f" * 64)
    assert c.run() == "STOPPED" and len(runc.calls) == 1
    BA.void_streak(C, BA.load_streak(C)["tripped"], "cause found and fixed")
    c2, runc2, _ = mk(ids, prior=[(A, a.bridge_code), (B, "e" * 64)], bridge_code="f" * 64, out=C)
    assert c2.run() == "END" and sorted(x[1] for x in runc2.calls) == sorted(ARMS)
    recs, source = _analysis_view([(A, a.bridge_code), (B, "e" * 64), (C, "f" * 64)], ids)
    assert all(recs[(ids[0], x)]["code"] == "SUB_NONEMPTY" for x in ARMS)
    assert all(source[f"{ids[0]}/{x}"] == os.path.abspath(C) for x in ARMS)
    _attempt_cap_ok([A, B, C], ids)


def test_acceptance_attempt_cap_on_existing_paths():
    # (3) A untouched and the cap intact: single-dir failure/rerun/void paths never exceed 2 counted attempts
    ids = ["a__a-1", "b__b-2"]; o = BA.arm_order(ids[0], ARMS)
    plan = {(ids[0], o[0]): ["crash", "crash"], (ids[0], o[1]): ["crash", "raise"], (ids[1], BA.arm_order(ids[1], ARMS)[0]): ["crash"]}
    b, run, out = mk(ids, plan=plan)
    st = b.run()
    if st == "STOPPED":
        BA.void_streak(out, BA.load_streak(out)["tripped"], "fixed")
        b2, _, _ = mk(ids, out=out)
        assert b2.run() == "END"
    _attempt_cap_ok([out], ids)
    per_slot = {}
    for c in run.calls:
        per_slot[c[:2]] = per_slot.get(c[:2], 0) + 1
    assert max(per_slot.values()) <= 2


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
