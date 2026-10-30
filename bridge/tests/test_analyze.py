"""判读入口的合成数据测试（v4）：从每槽 result.json 走到报告——R-a、Q1、Q2、R-e、W 稳健性、严格版、措辞义务、
分母与未知、熔断与保守附表、身份核对、版本分层、计价失败与资源中止的计数、漂移检查、三臂方案。
Run: python3 -I bridge/tests/test_analyze.py"""
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes as FK  # noqa: E402
import identity as ID  # noqa: E402
import analyze as AN  # noqa: E402
import prompts as P  # noqa: E402

B = 400
SEL = os.path.join(os.path.dirname(ID.HERE), "docs", "prereg", "2026-10-18_bridge_selection_DRAFT.json")
STRATA = AN.load_selection(SEL)
IDS = list(STRATA)                       # 60 hi then 40 lo
ARMS = list(P.ARMS)
CODE = "c" * 64


def code_for(s, h):
    return "SUB_NONEMPTY" if s == 1 else ("CAP_STEP_DIFF" if h == 1 else "CAP_STEP_NODIFF")


def write(spec, versions=lambda j, arm: ["gemini-3-flash-preview"], skip=lambda j, arm: False, ident=None,
          extra=lambda j, arm: {}):
    """spec[arm] = j -> (S, H, W); writes OUT/<id>/<arm>/result.json like evaluate.py."""
    out = FK.tmpdir()
    for j, iid in enumerate(IDS):
        for arm in ARMS:
            if skip(j, arm):
                continue
            s, h, w = spec.get(arm, spec["silent"])(j)
            d = os.path.join(out, iid, arm); os.makedirs(d)
            i = {"bridge_code_sha256": CODE, "d1_code_sha256": ID.D1_EXPECTED, "k": 50, "arm": arm, "instance_id": iid,
                 "image_id": FK.IMG, "dev": False, "model_versions": versions(j, arm)}
            i.update((ident or {}).get(arm, {}))
            json.dump({"instance_id": iid, "arm": arm, "identity": i, "terminal": True, "S": s, "H": h, "W": w,
                       "code": code_for(s, h) if None not in (s, h) else "ENG_FAIL", "resource_stop": False,
                       **extra(j, arm)},
                      open(os.path.join(d, "result.json"), "w"))
    return out


def run(spec, **kw):
    return AN.analyze(AN.load_results(write(spec, **kw), STRATA, ARMS, CODE), STRATA, ARMS, B)


ZERO = lambda j: (0, 0, 0)
BIG = lambda j: (1, 1, 1) if j % 10 < 7 else (0, 0, 0)          # 70% solved and submitted


def test_selection_loaded():
    assert len(IDS) == 100 and sum(1 for v in STRATA.values() if v == "hi") == 60


SAL = lambda j: (0, 1, 1) if j % 10 < 6 else (0, 0, 0)                               # silent: 60% salvaged at the cap
BOTH = lambda j: (1, 1, 1) if j % 10 < 7 else ((0, 1, 1) if j % 10 < 8 else (0, 0, 0))


def test_both_answers_reported_with_wording():
    r = run({"silent": SAL, "neutral": SAL, "number": BOTH, "bats": BOTH})
    assert (r["R-a"], r["Q1"], r["Q2"], r["W_robustness"]) == ("pass", "over_half", "positive", "both_products")
    assert r["R-e"]["row"] == ["pass", "over_half", "positive"]
    assert any("超过总收益的一半" in c for c in r["claims"]) and any("不等于“交付影响很小”" in c for c in r["claims"])
    assert r["dW_main_pair"] == "bats-silent" and "dW" in r["diffs"]["number-silent"]


def test_reversal_wording_tied_to_scoring_rule():
    sub3 = lambda j: (1, 1, 1) if j % 10 < 3 else (0, 0, 0)
    r = run({"silent": SAL, "neutral": SAL, "number": sub3, "bats": sub3})
    assert (r["R-a"], r["Q2"]) == ("pass", "negative") and any(f.startswith("REVERSAL") for f in r["flags"])
    assert any("自动收取评分（H）下告知收益为负" in c for c in r["claims"])
    assert not any("有害" in c and "评分" not in c for c in r["claims"])


def test_H_scored_harm_not_W_scored():
    # Q2 negative under H, but on the unified end-of-run workspace W the arms do not differ
    sub3 = lambda j: (1, 1, 1) if j % 10 < 3 else (0, 0, 0)
    r = run({"silent": lambda j: (0, 1, 0) if j % 10 < 6 else (0, 0, 0), "neutral": ZERO,
             "number": sub3, "bats": lambda j: (1, 1, 0) if j % 10 < 3 else (0, 0, 0)})
    assert r["Q2"] == "negative" and r["W_robustness"] == "H_only"


def test_near_zero_gain_rows():
    r = run({"silent": BIG})
    assert r["R-a"] == "below" and r["Q1"] == "not_evaluated" and r["R-e"]["row"] == ["below"]


def test_unknown_W_kept_separate_from_S_le_H():
    # 9 unknown W in bats (W unknown rate 9% <= 10%): the W robustness uses its own worst fill (W=0 for bats)
    r = run({"silent": SAL, "neutral": SAL, "number": BOTH,
             "bats": lambda j: (BOTH(j)[0], BOTH(j)[1], None) if j in range(70, 79) else BOTH(j)})
    assert r["W_unknown_rate"]["bats"] == 0.09 and r["unknown_rate"]["bats"] == 0.0 and not r["unknown_cap_triggered"]
    assert r["Q2"] == "positive" and r["descriptives"]["bats"]["W"]["unknown"] == 9


def test_missing_slots_are_unknown_and_fuse_blocks_everything():
    r = run({"silent": ZERO, "number": BIG, "bats": BIG}, skip=lambda j, arm: arm == "silent" and j < 11)
    assert r["unknown_rate"]["silent"] == 0.11 and r["unknown_cap_triggered"]
    assert (r["R-a"], r["Q1"], r["Q2"], r["R-c"], r["R-d"]) == ("undetermined",) * 5
    assert "工程报告" in r["R-e"]["next_step"] and r["descriptives"]["silent"]["codes"]["MISSING"] == 11
    assert r["descriptives"]["silent"]["not_run"] == 11 and r["claims"] == ["未知率熔断：不作任何科学结论"]
    assert "status" in r["conservative_appendix"] and r["conservative_appendix"]["R-a"]["result"] == "pass"


def test_unknowns_concentrated_in_capped_runs_of_one_arm():
    # 9 bats trajectories that ended at the cap with a correct workspace have H unknown (binary F / stray process):
    # below the fuse, but the least favourable fill removes them from bats' H
    r = run({"silent": SAL, "neutral": SAL, "number": BOTH,
             "bats": lambda j: (0, None, None) if j in (7, 17, 27, 37, 47, 57, 67, 77, 87) else BOTH(j)})
    assert r["unknown_rate"]["bats"] == 0.09 and not r["unknown_cap_triggered"]
    q2 = r["detail"]["Q2"]
    assert q2["point_range"][1] - q2["point_range"][0] > 0.05


def test_pricing_and_resource_stops_counted_not_dropped():
    def ex(j, arm):
        if arm == "bats" and j == 99:
            return {"pricing_failure": "UNREGISTERED_VERSION"}
        if arm == "bats" and j == 98:
            return {"resource_stop": True}
        return {}
    r = run({"silent": SAL, "neutral": SAL, "number": BOTH,
             "bats": lambda j: (None, None, None) if j in (98, 99) else BOTH(j)}, extra=ex)
    d = r["descriptives"]["bats"]
    assert d["pricing_failure"] == 1 and d["resource_stop"] == 1 and d["codes"]["ENG_FAIL"] == 2
    assert "cap_with_F_unavailable" in d
    assert r["unknown_rate"]["bats"] == 0.02 and d["n_slots"] == 100


def test_drift_check_against_public_synthetic_A():
    pub = os.path.join(os.path.dirname(ID.HERE), "data", "d1_census", "raw", "20260217_mini-v2.0.0_gemini-3-flash-high.json")
    out = write({"silent": ZERO})
    r = AN.analyze(AN.load_results(out, STRATA, ARMS, CODE), STRATA, ARMS, B, pub)
    dc = r["drift_check"]
    assert dc["n_tasks_compared"] == 100 and dc["missing_in_public"] == 0 and dc["affects_rules"] is False
    assert dc["table"].get("A1_S1", 0) == 0 and 0 < dc["A_weighted"] < 1


def test_identity_mismatch_fails_closed():
    for bad in ({"dev": True}, {"bridge_code_sha256": "0" * 64}, {"k": 49}, {"d1_code_sha256": "0" * 64}):
        out = write({"silent": ZERO}, ident={"bats": bad})
        try:
            AN.load_results(out, STRATA, ARMS, CODE)
        except SystemExit:
            continue
        raise AssertionError(f"accepted mismatching identity {bad}")


def test_version_change_reported_by_version():
    r = run({"silent": ZERO, "number": BIG, "bats": BIG},
            versions=lambda j, arm: ["v1"] if j < 50 else (["v2"] if j < 99 else ["v1", "v2"]))
    v = r["model_versions"]
    assert v["changed"] and v["distinct"] == ["v1", "v2"] and set(v["by_version"]) == {"v1", "v2", "mixed:v1|v2"}
    assert v["by_version"]["v1"]["bats"]["S"]["n"] == 50


def test_registry_changed_mid_run_is_flagged():
    reg = lambda px: {"confirmed_by_author": True, "relative_tolerance": 0.01, "provisional_usd_per_unpriced_call": 1.0,
                      "versions": {"gemini-3-flash-preview": {"input_per_token": px, "output_per_token": 3e-6, "cache_read_per_token": 5e-8}}}
    ok = run({"silent": SAL, "neutral": SAL, "number": BOTH, "bats": BOTH}, ident={a: {"pricing_registry": reg(5e-7)} for a in ARMS})
    assert ok["pricing_registry_check"]["consistent"] and not any(f.startswith("REGISTRY") for f in ok["flags"])
    bad = run({"silent": SAL, "neutral": SAL, "number": BOTH, "bats": BOTH},
              ident={**{a: {"pricing_registry": reg(5e-7)} for a in ARMS}, "bats": {"pricing_registry": reg(9e-7)}})
    assert not bad["pricing_registry_check"]["consistent"] and any(f.startswith("REGISTRY_INCONSISTENT") for f in bad["flags"])


def test_fb1_sensitivity_section():
    # 12 BATS slots unknown under F-v6 (fuse in the primary) but recovered by F-b1: primary undetermined, F-b1 decided
    def ex(j, arm):
        return {"H_b1": BOTH(j)[1], "W_b1": BOTH(j)[2], "F_b1_used": True} if arm == "bats" and j < 12 else {}
    r = run({"silent": SAL, "neutral": SAL, "number": BOTH,
             "bats": lambda j: (BOTH(j)[0], None, None) if j < 12 and BOTH(j)[0] == 0 else BOTH(j)}, extra=ex)
    fb = r["F_b1_sensitivity"]
    assert "不是恢复了原始截止状态" in fb["label"] and fb["n_slots_using_F_b1"] == 12
    assert fb["unknown_rate"]["bats"] < r["unknown_rate"]["bats"]


def test_three_arm_variant_has_no_R_d():
    out = write({"silent": SAL, "number": BOTH, "bats": BOTH})
    arms = ["silent", "number", "bats"]
    r = AN.analyze(AN.load_results(out, STRATA, arms, CODE), STRATA, arms, B)
    assert "R-d" not in r and r["Q1"] == "over_half" and set(r["diffs"]) == {"bats-silent", "bats-number", "number-silent"}


def test_multi_dir_results_each_with_own_identity():
    # addendum 2 (I2): an authorised mid-run code change splits results over two dirs; each dir is validated against
    # its own identity, the later dir wins for a slot present in both, and a wrong identity fails closed
    CODE2 = "d" * 64
    d_old = write({"silent": ZERO})
    d_new = write({"silent": BIG}, ident={a: {"bridge_code_sha256": CODE2} for a in ARMS})
    recs, source = AN.load_results_multi([(d_old, CODE), (d_new, CODE2)], STRATA, ARMS)
    assert all(v == os.path.abspath(d_new) for v in source.values()) and len(source) == 100 * len(ARMS)
    assert recs[(IDS[0], "silent")]["S"] == BIG(0)[0]
    for bad in ([(d_new, CODE)], [(d_old, CODE2)]):
        try:
            AN.load_results_multi(bad, STRATA, ARMS)
        except SystemExit:
            continue
        raise AssertionError(f"accepted results under a wrong identity: {bad}")


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
