"""评测步骤的离线测试（假 judge，不用 Docker）。Run: python3 -I bridge/tests/test_evaluate.py"""
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes as FK  # noqa: E402
import identity as ID  # noqa: E402
import evaluate as EV  # noqa: E402
from outcome import score, classify  # noqa: E402

D1_BEFORE = FK.d1_snapshot()


def ev(exit_status, judge=None, attempt="a1", **kw):
    t = FK.tmpdir(); judge = judge or FK.FakeJudge()
    rec_kw = {k: v for k, v in kw.items() if k in ("submission", "F", "limit_kind", "why", "versions", "dev", "bridge_code", "k")}
    FK.write_record(t, iid="x__y-1", arm="number", attempt="a1", exit_status=exit_status, **rec_kw)
    r = EV.evaluate(t, instance_id="x__y-1", arm="number", attempt=attempt, image_id=FK.IMG, judge_fn=judge,
                    dev=kw.get("dev_eval", False))
    assert json.load(open(os.path.join(t, "result.json"))) == json.loads(json.dumps(r, default=str))
    return r, judge


def test_score_table_all_codes():
    def s(code, rule, **k):
        x = score(code, rule, **k); return (x["S"], x["H"], x["W"])
    assert s(*classify(exit_status="Submitted", submission="diff FIX", limit_kind=None, workspace_patch=b"w", eng_fail=False),
             sub_verdict=1, f_verdict=0, f_available=True) == (1, 1, 0)
    assert s(*classify(exit_status="Submitted", submission="  \n", limit_kind=None, workspace_patch=b"w", eng_fail=False),
             f_verdict=1, f_available=True) == (0, 0, 1)
    assert s(*classify(exit_status="LimitsExceeded", submission="", limit_kind="step", workspace_patch=b"d", eng_fail=False),
             f_verdict=1, f_available=True) == (0, 1, 1)
    assert s(*classify(exit_status="LimitsExceeded", submission="", limit_kind="step", workspace_patch=b"", eng_fail=False),
             f_verdict=0, f_available=True) == (0, 0, 0)
    assert s(*classify(exit_status="LimitsExceeded", submission="", limit_kind="cost", workspace_patch=b"d", eng_fail=False),
             f_verdict=1, f_available=True) == (0, 1, 1)
    assert s(*classify(exit_status="FormatExit", submission="", limit_kind=None, workspace_patch=None, eng_fail=False)) == (0, None, None)
    assert s(*classify(exit_status="Submitted", submission="p", limit_kind=None, workspace_patch=b"w", eng_fail=True),
             sub_verdict=1, f_verdict=1, f_available=True) == (None, None, None)
    assert classify(exit_status="weird", submission="", limit_kind=None, workspace_patch=b"", eng_fail=False)[0] == "ENG_FAIL"


def test_submitted_nonempty_judges_submission_and_F():
    r, j = ev("Submitted", submission="diff FIX", F=b"diff BAD workspace")
    assert (r["code"], r["S"], r["H"], r["W"]) == ("SUB_NONEMPTY", 1, 1, 0) and len(j.calls) == 1 + 1
    assert r["verdicts"]["submission"]["verdict"] == 1 and r["verdicts"]["F"]["verdict"] == 0


def test_identical_products_judged_once():
    r, j = ev("Submitted", submission="diff FIX", F=b"diff FIX")
    assert (r["S"], r["H"], r["W"]) == (1, 1, 1) and len(j.calls) == 1 and r["n_unique_products_judged"] == 1


def test_cap_step_diff_uses_F_for_H_and_W():
    r, j = ev("LimitsExceeded", limit_kind="step", F=b"diff FIX")
    assert (r["code"], r["S"], r["H"], r["W"]) == ("CAP_STEP_DIFF", 0, 1, 1)
    assert j.calls[0]["tag"].startswith("x_y-1-number-a1-") and j.calls[0]["expected_id"] == FK.IMG


def test_cap_step_nodiff_is_zero_without_launch():
    r, j = ev("LimitsExceeded", limit_kind="step", F=b"")
    assert (r["code"], r["S"], r["H"], r["W"]) == ("CAP_STEP_NODIFF", 0, 0, 0) and j.calls == []


def test_cap_cost_with_failed_extraction_makes_H_and_W_unknown():
    r, j = ev("LimitsExceeded", limit_kind="cost", F=None)
    assert (r["code"], r["S"], r["H"], r["W"]) == ("CAP_COST", 0, None, None) and j.calls == []


def test_eng_fail_is_all_unknown_and_never_judged():
    r, j = ev("Submitted", submission="diff FIX", F=b"diff FIX", why=["BUDGET_DISPLAY_MISMATCH"])
    assert (r["code"], r["S"], r["H"], r["W"]) == ("ENG_FAIL", None, None, None) and j.calls == []
    assert "BUDGET_DISPLAY_MISMATCH" in r["why"]


def test_spend_stop_is_resource_stop_eng_fail():
    r, _ = ev("SpendStop", F=b"diff FIX")
    assert r["code"] == "ENG_FAIL" and r["resource_stop"] and r["S"] is None


def test_missing_record_is_eng_fail():
    t = FK.tmpdir(); j = FK.FakeJudge()
    r = EV.evaluate(t, instance_id="x__y-1", arm="bats", attempt="zz", image_id=FK.IMG, judge_fn=j)
    assert r["code"] == "ENG_FAIL" and "NO_RUN_RECORD" in r["why"] and j.calls == []


def test_judge_unknown_only_affects_that_verdict():
    r, _ = ev("Submitted", submission="diff FLAKY", F=b"diff FIX")
    assert (r["S"], r["H"], r["W"]) == (None, None, 1)
    r, _ = ev("Submitted", submission="diff FIX", F=b"diff BOOM")
    assert (r["S"], r["H"], r["W"]) == (1, 1, None) and r["verdicts"]["F"]["reason"] == "JUDGE_EXCEPTION"
    r, _ = ev("LimitsExceeded", limit_kind="step", F=b"diff FLAKY")
    assert (r["S"], r["H"], r["W"]) == (0, None, None)


def test_tampered_artifact_or_attempt_mismatch_is_eng_fail():
    t = FK.tmpdir()
    FK.write_record(t, iid="x__y-1", arm="number", attempt="a1", exit_status="LimitsExceeded", limit_kind="step", F=b"diff FIX")
    open(os.path.join(t, "F_end.patch"), "wb").write(b"diff BAD")
    r = EV.evaluate(t, instance_id="x__y-1", arm="number", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert r["code"] == "ENG_FAIL" and "F_ARTIFACT_INCONSISTENT" in r["why"]
    r, _ = ev("Submitted", submission="diff FIX", F=b"x", attempt="other")
    assert r["code"] == "ENG_FAIL" and "RUN_RECORD_MISMATCH" in r["why"]
    r, _ = ev("Submitted", submission="diff FIX", F=b"x", k=3)
    assert r["code"] == "ENG_FAIL" and "RUN_RECORD_K_OR_DEV_MISMATCH" in r["why"]
    r, _ = ev("Submitted", submission="diff FIX", F=b"x", bridge_code="0" * 64)
    assert r["code"] == "ENG_FAIL" and "CODE_IDENTITY_CHANGED_BETWEEN_RUN_AND_EVAL" in r["why"]


def test_identity_fields():
    r, _ = ev("Submitted", submission="diff FIX", F=b"x", versions=("gemini-3-flash-preview-001", "gemini-3-flash-preview-002"))
    i = r["identity"]
    assert i["d1_code_sha256"] == ID.D1_EXPECTED and i["bridge_code_sha256"] == ID.bridge_code_sha256()
    assert (i["arm"], i["instance_id"], i["k"], i["image_id"], i["dev"]) == ("number", "x__y-1", 50, FK.IMG, False)
    assert i["model_versions"] == ["gemini-3-flash-preview-001", "gemini-3-flash-preview-002"]
    assert i["judge"] == "judge-v2" and i["F"] == "F-v6" and EV.identity_matches(r, i)


def test_d1_untouched():
    assert ID.d1_code_sha256() == ID.D1_EXPECTED and FK.d1_snapshot() == D1_BEFORE


def test_blind_submit_heuristic():
    from outcome import blind_submit as BS, commands_from_messages
    sub = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt"
    assert BS(["ls", "sed -i 's/a/b/' x.py", "git diff > patch.txt", sub]) is True
    assert BS(["sed -i 's/a/b/' x.py", "python -m pytest tests/x.py", "git diff > patch.txt", sub]) is False
    assert BS(["sed -i 's/a/b/' x.py && python repro.py", sub]) is False          # test after the write, same command
    assert BS(["python repro.py && sed -i 's/a/b/' x.py", sub]) is True           # write after the last test
    assert BS(["python repro.py > /dev/null 2>&1", sub]) is None                  # nothing written before submit
    assert BS(["cat > x.py << 'EOF'", "ls"]) is None                              # never submitted
    assert BS(["python - <<'EOF'\nopen('x.py','w').write('1')\nEOF", sub]) is True    # python used as an editor
    msgs = [{"role": "assistant", "extra": {"actions": [{"command": "ls"}, {"command": sub}]}}, {"role": "user"}]
    assert commands_from_messages(msgs) == ["ls", sub]


if __name__ == "__main__":
    FK.run_tests(globals())
