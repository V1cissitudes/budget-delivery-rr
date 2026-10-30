"""Synthetic tests for bridge/analysis_rules.py (prereg DRAFT v4 §6). Rule-level counterexamples: both delivery answers
at once, reversal across scoring rules, near-zero total gain, opposite layers, unknowns concentrated in capped
trajectories and differing by arm, zero / few events, a missing arm, W robustness, strict report, R-e completeness.
Run: python3 -I bridge/tests/test_analysis_rules.py"""
import os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes as FK  # noqa: E402
import analysis_rules as AR  # noqa: E402

B = 1500
ALLOWED = {"R-a": {"pass", "below", "undetermined"},
           "Q1": {"over_half", "under_half", "undetermined", "not_evaluated"},
           "Q2": {"positive", "negative", "undetermined", "not_evaluated"}}


def make(sil, bats, num=None, neu=None, w=None):
    """sil/bats/num/neu: j -> (S, H) for task j (60 hi + 40 lo); w: arm -> (j -> W) (default W = H)."""
    d, wd = {}, {}
    fns = {"silent": sil, "bats": bats, "number": num or sil}
    if neu is not False:
        fns["neutral"] = neu or sil
    for j in range(100):
        st = "hi" if j < 60 else "lo"
        d[f"t{j}"] = {"stratum": st}; wd[f"t{j}"] = {"stratum": st}
        for a, f in fns.items():
            s, h = f(j)
            d[f"t{j}"][a] = (s, h)
            ww = (w or {}).get(a, lambda j, h=h: h)(j)
            wd[f"t{j}"][a] = (ww, ww)
    return d, wd


def decide(d, wd=None):
    out = AR.decide(d, wd, B)
    for k, vals in ALLOWED.items():
        assert out[k] in vals, (k, out[k])
    return out


def test_cp_upper_zero_events_matches_known_bound():
    assert abs(AR.cp_upper(0, 100, 0.05) - (1 - 0.05 ** (1 / 100))) < 1e-6
    d, _ = make(lambda j: (0, 0), lambda j: (0, 0))
    assert abs(AR.u_exact_salvage(d) - (288 / 499 * (1 - 0.025 ** (1 / 60)) + 211 / 499 * (1 - 0.025 ** (1 / 40)))) < 1e-6


def test_over_half_and_positive_residual_both_reported():
    # silent: 60% salvaged at the cap (S0H1); bats: 70% submitted correct, 80% correct workspace.
    # T = 0.7, ΔH = 0.2, J = 0.5 = 0.71T  -> Q1 over_half AND Q2 positive; neither hides the other
    d, wd = make(lambda j: (0, 1) if j % 10 < 6 else (0, 0),
                 lambda j: (1, 1) if j % 10 < 7 else ((0, 1) if j % 10 < 8 else (0, 0)))
    out = decide(d, wd)
    assert (out["R-a"], out["Q1"], out["Q2"]) == ("pass", "over_half", "positive"), out
    assert any(f.startswith("BOTH") for f in out["flags"]) and any(f.startswith("RESIDUAL_NOT_SMALL") for f in out["flags"])
    assert out["R-e"]["row"] == ["pass", "over_half", "positive"] and out["W_robustness"] == "both_products"


def test_S_gain_with_H_loss_is_a_reversal():
    # silent: 60% salvage; bats: submits 30% correct, no salvage. T = +0.3, ΔH = −0.3 -> reversal, both directions
    d, wd = make(lambda j: (0, 1) if j % 10 < 6 else (0, 0), lambda j: (1, 1) if j % 10 < 3 else (0, 0))
    out = decide(d, wd)
    assert (out["R-a"], out["Q1"], out["Q2"]) == ("pass", "over_half", "negative"), out
    assert any(f.startswith("REVERSAL") for f in out["flags"])
    assert "反转" in out["R-e"]["next_step"]


def test_near_zero_total_gain():
    same = lambda j: (1, 1) if j % 10 < 3 else ((0, 1) if j % 10 < 6 else (0, 0))
    out = decide(*make(same, same))
    assert out["R-a"] == "below" and out["Q1"] == out["Q2"] == "not_evaluated" and out["R-e"]["row"] == ["below"]
    assert out["R-c"] == "below" and out["R-d"] == "below"


def test_opposite_layers_S_down_H_up():
    # bats never submits but leaves more correct workspaces: T = −0.3 (S-scored harm), ΔH = +0.3
    d, wd = make(lambda j: (1, 1) if j % 10 < 3 else (0, 0), lambda j: (0, 1) if j % 10 < 6 else (0, 0))
    out = decide(d, wd)
    assert out["R-a"] == "below" and out["Q2"] == "not_evaluated"
    assert any(f.startswith("DESCRIPTIVE_S_NEGATIVE") for f in out["flags"])
    assert out["detail"]["descriptive_dH_bats_silent"]["result"] == "positive"


def test_unknowns_in_capped_bats_trajectories_least_favourable():
    # Same truth as the 'both' test, but 8 bats trajectories that would be S0H1 at the cap have H unknown
    # (e.g. binary F); complete-case would see ΔH > 0, the least favourable fill (bats H = 0) must be used.
    base_b = lambda j: (1, 1) if j % 10 < 3 else ((0, 1) if j % 10 < 8 else (0, 0))
    unk = {j for j in range(100) if 3 <= j % 10 < 8}
    unk = set(sorted(unk)[:8])
    d, wd = make(lambda j: (0, 1) if j % 10 < 5 else (0, 0), lambda j: (0, None) if j in unk else base_b(j))
    out = decide(d, wd)
    assert out["unknown_rate"]["bats"] == 0.08 and not out["fuse"]
    lo = AR.fill(d, AR.diff("bats", "silent", 1), "min")
    assert all(lo[f"t{j}"]["bats"] == (0, 0) for j in unk)
    assert out["detail"]["Q2"]["point_range"][0] < out["detail"]["Q2"]["point_range"][1]


def test_unknowns_above_fuse_in_one_arm_only():
    d, wd = make(lambda j: (0, 1) if j % 10 < 6 else (0, 0),
                 lambda j: (0, None) if j < 11 else ((1, 1) if j % 10 < 7 else (0, 0)))
    out = decide(d, wd)
    assert out["fuse"] and out["unknown_rate"]["bats"] == 0.11 and out["unknown_rate"]["silent"] == 0
    assert out["R-a"] == out["Q1"] == out["Q2"] == out["R-c"] == out["R-d"] == "undetermined"
    assert out["R-e"]["row"] == ["fuse"] and "status" in out["conservative_appendix"]
    assert any(f.startswith("FUSE") for f in out["flags"])


def test_zero_salvage_events_use_exact_bound_only():
    # nobody is salvaged in silent (H == S); bats solves 70% -> bootstrap of R0 degenerate; only the exact bound
    d, _ = make(lambda j: (0, 0), lambda j: (1, 1) if j % 10 < 7 else (0, 0))
    out = decide(d)
    q1 = out["detail"]["Q1"]
    assert q1["silent_events_max_fill"] == 0 and q1["result"] == "under_half" and q1["path"] == "exact_R0_bound"
    d, _ = make(lambda j: (0, 0), lambda j: (1, 1) if j % 10 < 2 or j % 25 == 3 else (0, 0))  # T ~ 0.2
    q1 = decide(d)["detail"]["Q1"]
    assert q1["path"] != "bootstrap" and q1["result"] in ("under_half", "undetermined")


def test_few_events_never_over_half():
    # 3 silent salvage events: J <= R0 <= 3%, the bootstrap is not trusted for 'over_half'
    d, _ = make(lambda j: (0, 1) if j in (0, 1, 2) else ((1, 1) if j % 10 == 9 else (0, 0)),
                lambda j: (1, 1) if j in (0, 1, 2) or j % 10 >= 6 else (0, 0))
    out = decide(d)
    assert out["R-a"] == "pass" and out["Q1"] != "over_half" and out["detail"]["Q1"]["silent_events_min_fill"] == 3


def test_three_arm_design_and_arm_never_run():
    d, wd = make(lambda j: (0, 1) if j % 10 < 6 else (0, 0), lambda j: (1, 1) if j % 10 < 7 else (0, 0), neu=False)
    out = decide(d, wd)
    assert "R-d" not in out and out["R-a"] == "pass"
    d, wd = make(lambda j: (0, 0), lambda j: (1, 1), num=lambda j: (None, None))       # number arm never ran
    out = decide(d, wd)
    assert out["fuse"] and out["unknown_rate"]["number"] == 1.0 and out["R-a"] == "undetermined"


def test_W_robustness_H_only_and_W_fuse():
    sil = lambda j: (0, 1) if j % 10 < 6 else (0, 0)
    bats = lambda j: (1, 1) if j % 10 < 7 else ((0, 1) if j % 10 < 8 else (0, 0))
    # W: bats' end-of-run workspace after submission fails the tests in 40% of tasks (extra files break tests)
    out = decide(*make(sil, bats, w={"bats": lambda j: 1 if j % 10 < 3 else 0}))
    assert out["Q2"] == "positive" and out["W_robustness"] == "H_only"
    out = decide(*make(sil, bats, w={"bats": lambda j: None if j < 11 else (1 if j % 10 < 8 else 0)}))
    assert out["Q2"] == "positive" and out["W_robustness"] == "undetermined" and out["W_unknown_rate"]["bats"] == 0.11


def test_strict_report_is_report_only():
    # silent salvage 30%, bats submits exactly those: ΔH = 0 exactly, J = T
    d, wd = make(lambda j: (0, 1) if j % 10 < 3 else (0, 0), lambda j: (1, 1) if j % 10 < 3 else (0, 0))
    out = decide(d, wd)
    assert out["Q1"] == "over_half" and out["Q2"] == "undetermined" and out["strict_holds"]
    assert out["R-e"]["row"] == ["pass", "over_half", "undetermined"]


def test_q2_needs_enough_discordant_pairs():
    # bats better on H in only 4 tasks: the bootstrap may exclude 0 but the exact sign test (p = 1/16) does not
    sil = lambda j: (1, 1) if j % 10 < 3 else (0, 0)
    out = decide(*make(sil, lambda j: (1, 1) if j % 10 < 3 or j in (5, 15, 25, 35) else sil(j)))
    assert out["detail"]["descriptive_dH_bats_silent"]["result"] == "undetermined"


def test_R_e_covers_every_combination_and_no_little_branch():
    for q1 in ("over_half", "under_half", "undetermined"):
        for q2 in ("positive", "negative", "undetermined"):
            assert AR.next_step("pass", q1, q2, False)["next_step"]
    for ra in ("below", "undetermined"):
        assert AR.next_step(ra, "not_evaluated", "not_evaluated", False)["row"] == [ra]
    assert AR.next_step("pass", "over_half", "positive", True)["row"] == ["fuse"]
    assert "little" not in str(AR.R_E) and not hasattr(AR, "MIN_EVENTS_MOST")


def test_bonferroni_option_widens_q_intervals():
    d, wd = make(lambda j: (0, 1) if j % 10 < 6 else (0, 0),
                 lambda j: (1, 1) if j % 10 < 7 else ((0, 1) if j % 10 < 8 else (0, 0)))
    a = AR.decide(d, wd, B)["detail"]["Q2"]["L"]
    old = AR.ALPHA_Q; AR.ALPHA_Q = 0.025
    try:
        b = AR.decide(d, wd, B)["detail"]["Q2"]["L"]
    finally:
        AR.ALPHA_Q = old
    assert b <= a


def test_q1_event_gate_ignores_unknowns():
    # review C3-1 (Codex counterexample): 4 confirmed silent salvage events; turning one (0,0) into (None,None)
    # must NOT switch Q1 from 'undetermined' to 'under_half'
    def sil(j):
        return (0, 1) if j < 4 else (0, 0)
    def bats(j):
        return (1, 1) if (j < 20 or 60 <= j < 72) else (0, 0)
    base = decide(*make(sil, bats))
    unk = decide(*make(lambda j: (None, None) if j == 30 else sil(j), bats))
    q0, q1 = base["detail"]["Q1"], unk["detail"]["Q1"]
    assert (base["R-a"], base["Q1"], q0["silent_events_min_fill"]) == ("pass", "undetermined", 4)
    assert q1["silent_events_max_fill"] == 5 and q1["U(J-0.5T)"] < 0          # the old ev_max gate would flip here
    assert (unk["Q1"], q1["silent_events_min_fill"], q1["path"]) == ("undetermined", 4, None)


def test_descriptive_dH_uses_q_level():
    # review C3-2: the ungated descriptive Δ_H uses the same level as Q2
    same = lambda j: (1, 1) if j % 10 < 3 else (0, 0)
    out = AR.decide(*make(same, same), B=B)
    d = out["detail"]["descriptive_dH_bats_silent"]
    assert d["result"] == "undetermined" and AR.RULES_VERSION == "bridge-rules-v4.1"


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
