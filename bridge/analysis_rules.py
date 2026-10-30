"""Bridge experiment decision rules, prereg DRAFT v4 §4 + §6 (DRAFT, not sealed; every threshold is a CANDIDATE value
awaiting the author unless the prereg marks it as fixed).

Data model
- data[task_id]  = {"stratum": "hi"|"lo", arm: (S, H), ...}; S, H in {0, 1, None}; None = unknown or not run.
- wdata[task_id] = {"stratum": ..., arm: (W, W), ...}: W stored twice so the same fill / bootstrap code applies; an
  unknown W has no ordering constraint and may be filled with 0 or 1 (W is NOT tied to S or H, prereg v4 §4 rule 5b).
- Arms: "silent", "number", "bats", optionally "neutral". Population stratum sizes come from the selection file.

Principles (v4)
- Every claim is evaluated under the fill of unknowns least favourable to THAT claim (S <= H respected for (S, H); W
  filled on its own). A claim holds only if it holds under its own least favourable fill.
- Intervals: stratified, task-paired percentile bootstrap. All quantities on one data set reuse the same resamples,
  which preserves the pairing between arms; the resulting intervals are MARGINAL intervals (one per quantity), not a
  simultaneous/joint confidence region (v4 correction of the v2/v3 wording).
- Primary test: R-a (large-effect screen on T = ΔS(BATS−Silent)). The two delivery questions Q1 and Q2 are tested
  only when R-a passes (fixed sequence). Q1 and Q2 are separate claims that may hold together; neither hides the other.
  Secondary: R-c, R-d. Report-only: ΔW robustness label, strict ±5pp report, Number−Silent ΔH/ΔW, reversal notes.
- Unknown-rate fuse (fixed at 10% by the author): any arm with S-or-H unknown above 10% => every rule "undetermined";
  the same computations are then attached as a labelled conservative appendix that is NOT a conclusion.
"""
import math, random

POP = {"hi": 288, "lo": 211}
THRESH = 0.10           # CANDIDATE: large-effect screening threshold (R-a, R-c, R-d)
MAX_UNKNOWN = 0.10      # FIXED (author 2026-10-19): any arm above this => every rule undetermined
MIN_EVENTS = 5          # CANDIDATE: silent-arm salvage events needed before the bootstrap is trusted for Q1
ALPHA = 0.05            # CANDIDATE: two-sided level of every percentile interval (one-sided 0.025 per claim)
ALPHA_Q = 0.025         # CANDIDATE (recommended): two-sided level of the Q1 and Q2 intervals (97.5%). Nominal meaning: 1.25%
                        # per direction; each question reports either direction (2.5% per question); over both questions'
                        # four directions the Bonferroni nominal bound is 5%. Percentile-bootstrap coverage is approximate.
SIGN_P = 0.025          # CANDIDATE: auxiliary conservative gate for Q2 / ΔW (unweighted exact sign test on discordant pairs).
                        # NOT an exact test of the weighted population effect and does not repair bootstrap coverage.
STRICT_BAND = 0.05      # CANDIDATE: report-only strict version, Δ_H interval inside [−5, +5]pp (closed)
STRICT_SHARE = 0.8      # CANDIDATE: report-only strict version, L(J − 0.8T) > 0
CANDS = [(0, 0), (0, 1), (1, 1)]
RULES_VERSION = "bridge-rules-v4.1"   # addendum 1 (2026-10-20): C3-1 event gate, C3-2 descriptive level


# ---------------------------------------------------------------- fills
def feasible(s, h):
    if s is not None and h is not None:
        return [(s, h)]
    if s is None and h is None:
        return CANDS
    if s is None:
        return [(x, h) for x in (0, 1) if x <= h]
    return [(s, y) for y in (0, 1) if y >= s]


def fill(data, coef, sense):
    """coef[arm] = (cS, cH): objective = sum over arms of cS*mean(S) + cH*mean(H). sense='min' fills each unknown so
    the objective is as small as possible (claims of the form 'lower bound > x'), 'max' the opposite. For wdata the
    (W, W) pairs make the fill choose W in {0, 1} freely."""
    pick = min if sense == "min" else max
    out = {}
    for t, row in data.items():
        new = {"stratum": row["stratum"]}
        for arm, (s, h) in ((a, v) for a, v in row.items() if a != "stratum"):
            cs, ch = coef.get(arm, (0, 0))
            new[arm] = pick(feasible(s, h), key=lambda p: cs * p[0] + ch * p[1])
        out[t] = new
    return out


def wmean(rows, f):
    tot = sum(POP.values()); acc = 0.0
    for st, Nh in POP.items():
        xs = [f(r) for r in rows if r["stratum"] == st]
        acc += Nh / tot * (sum(xs) / len(xs))
    return acc


def objective(rows, coef):
    return sum(wmean(rows, lambda r, a=a, c=c: c[0] * r[a][0] + c[1] * r[a][1]) for a, c in coef.items())


def diff(a, b, which):
    """coef for mean(which of arm a) - mean(which of arm b); which 0 = S, 1 = H (or W in wdata)."""
    ca = (1, 0) if which == 0 else (0, 1)
    return {a: ca, b: (-ca[0], -ca[1])}


# ---------------------------------------------------------------- bootstrap
class Boot:
    """Stratified, task-paired percentile bootstrap. Resample indices are drawn once per data set (fixed seed) and
    reused for every quantity, so arms stay paired within a task. Pure Python (Mac `python3 -I` and desktop)."""

    def __init__(self, data, B=10000, seed=20261019):
        self.ids = {st: sorted(t for t, r in data.items() if r["stratum"] == st) for st in POP}
        rng = random.Random(seed)
        self.idx = {st: [[rng.randrange(len(self.ids[st])) for _ in self.ids[st]] for _ in range(B)] for st in POP}
        self.B, tot = B, sum(POP.values())
        self.w = {st: POP[st] / tot for st in POP}

    def values(self, filled, coef):
        v = {st: [sum(c[0] * filled[t][a][0] + c[1] * filled[t][a][1] for a, c in coef.items()) for t in self.ids[st]]
             for st in POP}
        out = []
        for b in range(self.B):
            acc = 0.0
            for st in POP:
                vs, ix = v[st], self.idx[st][b]
                acc += self.w[st] * sum(vs[i] for i in ix) / len(ix)
            out.append(acc)
        out.sort()
        return out

    def ci(self, filled, coef, alpha=ALPHA):
        vals = self.values(filled, coef); B = len(vals)
        return vals[int(alpha / 2 * B)], vals[int((1 - alpha / 2) * B) - 1]


def lower(boot, data, coef, alpha=ALPHA):
    """(L, point) under the fill least favourable to 'L > x'."""
    d = fill(data, coef, "min")
    return boot.ci(d, coef, alpha)[0], objective(list(d.values()), coef), d


def upper(boot, data, coef, alpha=ALPHA):
    """(U, point) under the fill least favourable to 'U < x'."""
    d = fill(data, coef, "max")
    return boot.ci(d, coef, alpha)[1], objective(list(d.values()), coef), d


# ---------------------------------------------------------------- exact helpers
def cp_upper(x, n, alpha):
    """One-sided Clopper-Pearson upper bound at level 1-alpha."""
    if x >= n:
        return 1.0
    cdf = lambda p: sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(x + 1))
    lo, hi = x / n, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) > alpha else (lo, mid)
    return hi


def u_exact_salvage(data, arm="silent", alpha_each=0.025):
    """Stratified conservative upper bound on the arm's salvage rate E[H-S]: per-stratum one-sided CP upper bound at
    level 1-0.025 each (Bonferroni over the two strata, so alpha = 0.05/2 per stratum), combined with population
    weights. Unknowns filled to maximise salvage. Zero events: 1-0.025^(1/n) per stratum (60: 5.96%, 40: 8.81%)."""
    d = fill(data, {arm: (-1, 1)}, "max"); tot = sum(POP.values()); u = 0.0
    for st, Nh in POP.items():
        rs = [r[arm] for r in d.values() if r["stratum"] == st]
        u += Nh / tot * cp_upper(sum(h - s for s, h in rs), len(rs), alpha_each)
    return u


def salvage_events(filled, arm="silent"):
    return sum(h - s for s, h in (r[arm] for r in filled.values()))


def sign_test(filled, a, b, which, direction):
    """Unweighted exact one-sided sign test on discordant task pairs of arm a vs arm b (which 0 = S, 1 = H/W).
    direction +1: H1 'a > b'; -1: H1 'a < b'. Returns (n_for, n_against, p)."""
    pos = sum(1 for r in filled.values() if r[a][which] > r[b][which])
    neg = sum(1 for r in filled.values() if r[a][which] < r[b][which])
    k, n = (pos, pos + neg) if direction > 0 else (neg, pos + neg)
    p = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0
    return k, n - k, p


def unknown_rate(data, arm):
    return sum(1 for r in data.values() if None in r[arm]) / len(data)


# ---------------------------------------------------------------- rules
def screen(boot, data, a, b):
    """R-a / R-c / R-d: 'pass' / 'below' / 'undetermined' for the large-effect screen on ΔS(a−b)."""
    c = diff(a, b, 0)
    lo, pt_min, _ = lower(boot, data, c)
    hi, pt_max, _ = upper(boot, data, c)
    if pt_min >= THRESH and lo > 0:
        res = "pass"
    elif hi < THRESH:
        res = "below"
    else:
        res = "undetermined"
    return {"result": res, "L": lo, "U": hi, "point_range": [pt_min, pt_max]}


def q1_interaction_share(boot, data):
    """Q1: does the delivery-rule interaction J = T − Δ_H = R0 − R1 exceed half of the total gain T?
    J − 0.5T = 0.5·ΔS − ΔH. 'over_half': L > 0 and >= MIN_EVENTS silent salvage events (under that fill).
    'under_half': if the silent arm has >= MIN_EVENTS CONFIRMED salvage events (unknowns never counted as events;
    addendum 1, review C3-1), U(J − 0.5T) < 0; otherwise
    (rare events, bootstrap untrustworthy) the bound J <= R0 is used as a SUFFICIENT condition: U_exact(R0) < 0.5·L(T),
    with the one-sided ALPHA_Q/2 split between the two bounds (U_exact per stratum ALPHA_Q/8, L(T) one-sided ALPHA_Q/4).
    v4: the v3 'little' branch and its necessary condition U_exact(R0) < 0.25·L(T) are withdrawn (see prereg §6)."""
    c = {"bats": (0.5, -1), "silent": (-0.5, 1)}
    L, pt_min, dmin = lower(boot, data, c, ALPHA_Q)
    U, pt_max, dmax = upper(boot, data, c, ALPHA_Q)
    ev_min, ev_max = salvage_events(dmin), salvage_events(dmax)
    LT = lower(boot, data, diff("bats", "silent", 0), ALPHA_Q)[0]
    ux = u_exact_salvage(data)
    # exact path: split the one-sided ALPHA_Q/2 between the two bounds so the claim keeps its nominal level
    # (review item 5): U_exact per stratum at ALPHA_Q/8 (two strata => ALPHA_Q/4) and L(T) one-sided at ALPHA_Q/4
    ux_q = u_exact_salvage(data, alpha_each=ALPHA_Q / 8)
    LT_q = lower(boot, data, diff("bats", "silent", 0), ALPHA_Q / 2)[0]
    if L > 0 and ev_min >= MIN_EVENTS:
        res, path = "over_half", "bootstrap"
    elif ev_min >= MIN_EVENTS and U < 0:        # review C3-1: gate on CONFIRMED salvage events (unknowns not counted)
        res, path = "under_half", "bootstrap"
    elif ev_min < MIN_EVENTS and ux_q < 0.5 * LT_q:
        res, path = "under_half", "exact_R0_bound"
    else:
        res, path = "undetermined", None
    return {"result": res, "path": path, "L(J-0.5T)": L, "U(J-0.5T)": U, "point_range": [pt_min, pt_max],
            "silent_events_min_fill": ev_min, "silent_events_max_fill": ev_max, "U_exact(R0)": ux, "L(T)": LT,
            "exact_path": {"U_exact(R0)_alpha_each": ALPHA_Q / 8, "U_exact(R0)": ux_q, "L(T)_one_sided_alpha": ALPHA_Q / 4, "L(T)": LT_q}}


def signed_claim(boot, data, a, b, alpha=None):
    """Δ(a−b) of the second component (H in data, W in wdata): 'positive' if L > 0 and the exact sign test favours a;
    'negative' if U < 0 and the sign test favours b; otherwise 'undetermined'. Each side uses its own worst fill."""
    alpha = ALPHA_Q if alpha is None else alpha
    c = diff(a, b, 1)
    L, pt_min, dmin = lower(boot, data, c, alpha)
    U, pt_max, dmax = upper(boot, data, c, alpha)
    sp = sign_test(dmin, a, b, 1, +1); sn = sign_test(dmax, a, b, 1, -1)
    if L > 0 and sp[2] < SIGN_P:
        res = "positive"
    elif U < 0 and sn[2] < SIGN_P:
        res = "negative"
    else:
        res = "undetermined"
    return {"result": res, "L": L, "U": U, "point_range": [pt_min, pt_max],
            "sign_test_pos": {"for": sp[0], "against": sp[1], "p": sp[2]},
            "sign_test_neg": {"for": sn[0], "against": sn[1], "p": sn[2]}}


def strict_report(boot, data):
    """Report-only, low power: L(J − 0.8T) > 0 and the Δ_H(BATS−Silent) interval inside [−5, +5]pp (closed), each
    under its least favourable fill. Not reaching it is NOT evidence against 'mainly via delivery'."""
    s = 1 - STRICT_SHARE
    c = {"bats": (s, -1), "silent": (-s, 1)}
    L = lower(boot, data, c)[0]
    cH = diff("bats", "silent", 1)
    Lh, Uh = lower(boot, data, cH)[0], upper(boot, data, cH)[1]
    return {"L(J-0.8T)": L, "dH_bats_silent_CI": [Lh, Uh],
            "holds": bool(L > 0 and Lh >= -STRICT_BAND and Uh <= STRICT_BAND), "affects_R-e": False,
            "note": "低功效次要报告；未达到不能解释为“不等效”或“不主要来自交付”"}


def w_robustness(boot, wdata, q2, w_unknown):
    """Report-only robustness label for a determinate Q2: does the same sign hold when every trajectory is scored on
    its end-of-run workspace (W), i.e. one product per trajectory? Not a planning-mechanism claim."""
    if q2 not in ("positive", "negative"):
        return {"label": "not_applicable", "reason": "Q2 not determinate"}
    if max(w_unknown.get("bats", 0), w_unknown.get("silent", 0)) > MAX_UNKNOWN:
        return {"label": "undetermined", "reason": "W unknown rate > 10% in bats or silent"}
    w = signed_claim(boot, wdata, "bats", "silent")
    same = w["result"] == q2
    return {"label": "both_products" if same else "H_only", "dW": w}


def decide(data, wdata=None, B=10000, boot=None):
    """Full v4 rule set. Returns a dict with R-a, Q1, Q2, R-c, R-d, W robustness, strict report, flags and R-e."""
    arms = [a for a in data[next(iter(data))] if a != "stratum"]
    boot = boot or Boot(data, B)
    ur = {a: unknown_rate(data, a) for a in arms}
    wur = {a: unknown_rate(wdata, a) for a in arms} if wdata else {}
    fuse = any(v > MAX_UNKNOWN for v in ur.values())
    core = _rules(boot, data, wdata, arms, wur)
    out = {"rules_version": RULES_VERSION, "unknown_rate": ur, "W_unknown_rate": wur, "fuse": fuse}
    if fuse:
        out.update({"R-a": "undetermined", "Q1": "undetermined", "Q2": "undetermined", "R-c": "undetermined",
                    "W_robustness": "undetermined", "strict_holds": False,
                    "conservative_appendix": dict(core, status="熔断后的保守附表：不是结论，只供读者判断未知值的影响")})
        if "neutral" in arms:
            out["R-d"] = "undetermined"
    else:
        out.update({k: core[k]["result"] if isinstance(core.get(k), dict) and "result" in core[k] else core.get(k)
                    for k in ("R-a", "Q1", "Q2", "R-c", "R-d") if k in core})
        out["W_robustness"] = core["W_robustness"]["label"] if isinstance(core["W_robustness"], dict) else core["W_robustness"]
        out["strict_holds"] = bool((core.get("strict") or {}).get("holds"))
        out["detail"] = core
    out["flags"] = flags(out, core)
    out["R-e"] = next_step(out["R-a"], out["Q1"], out["Q2"], fuse)
    return out


def _rules(boot, data, wdata, arms, wur):
    core = {"R-a": screen(boot, data, "bats", "silent")}
    if core["R-a"]["result"] == "pass":
        core["Q1"] = q1_interaction_share(boot, data)
        core["Q2"] = signed_claim(boot, data, "bats", "silent")
        core["W_robustness"] = w_robustness(boot, wdata, core["Q2"]["result"], wur) if wdata else "not_computed"
        core["strict"] = strict_report(boot, data)
    else:
        core["Q1"] = core["Q2"] = {"result": "not_evaluated"}
        core["W_robustness"] = {"label": "not_applicable", "reason": "R-a did not pass"}
        core["strict"] = {"holds": False, "note": "R-a 未达门槛，不计算"}
        # descriptive only (not gated): sign of the gain under each scoring rule
        core["descriptive_dH_bats_silent"] = signed_claim(boot, data, "bats", "silent", ALPHA_Q)   # same level as Q2 (C3-2)
    core["R-c"] = screen(boot, data, "bats", "number")
    if "neutral" in arms:
        core["R-d"] = screen(boot, data, "number", "neutral")
    return core


def flags(out, core):
    """Wording obligations. 'Harm' is always tied to a scoring rule."""
    f = []
    ra, q2 = out["R-a"], out["Q2"]
    if ra == "pass" and q2 == "negative":
        f.append("REVERSAL: 原生交付（S）评分下告知收益达到筛查门槛，自动收取（H）评分下收益为负；两个方向都要报告")
    if ra == "pass" and out["Q1"] == "over_half" and q2 == "positive":
        f.append("BOTH: 交付规则交互超过总收益一半，同时自动收取后仍有正的残余收益；两者都要写")
    if ra == "pass" and q2 == "positive":
        f.append("RESIDUAL_NOT_SMALL_DELIVERY: “仍有残余收益”不等于“交付影响很小”")
    d = core.get("descriptive_dH_bats_silent") if not out["fuse"] else None
    if d and d["result"] == "negative":
        f.append("DESCRIPTIVE_H_NEGATIVE: 未经门控的描述：自动收取（H）评分下收益为负（区间上限 < 0 且符号检验 p < 0.025）")
    if not out["fuse"] and core["R-a"]["U"] < 0:
        f.append("DESCRIPTIVE_S_NEGATIVE: 原生交付（S）评分下收益为负（描述）")
    if out["fuse"]:
        f.append("FUSE: 至少一臂未知率超过 10%，所有规则判未定，只出工程报告与保守附表")
    return f


R_E = {
    "fuse": "未知率超过 10%：所有规则判未定，只出工程报告；保守附表照附但不作结论；不扩大实验",
    ("below",): "告知收益没有达到大效应门槛：先排查与 BATS 的差异（模型版本、提示、协议），不扩大实验",
    ("undetermined",): "不扩大实验，按“未定”报告；是否另做实验由作者决定并书面写明理由",
    ("over_half", "positive"): "提议做 Devstral 核心实验（规模另定）：交付交互过半且残余为正，两者都写",
    ("over_half", "undetermined"): "提议做 Devstral 核心实验（规模另定）：交付交互过半；残余收益未定",
    ("over_half", "negative"): "提议做 Devstral 核心实验（规模另定）：效果随评分规则反转（自动收取评分下为负），反转本身要报告",
    ("under_half", "positive"): "不以交付交互为主线扩大实验；写边界结果：交付交互不到总收益一半、自动收取后仍有正残余；不写“交付影响很小”",
    ("under_half", "undetermined"): "不以交付交互为主线扩大实验；写边界结果：交付交互不到总收益一半，残余收益未定",
    ("under_half", "negative"): "规则输出自相矛盾（交互不到一半却残余为负）：不下结论，检查实现与数据，报告作者",
    ("undetermined", "positive"): "不自动扩大；报告：自动收取后仍有正残余，交付交互占比未定；是否另做实验由作者书面决定",
    ("undetermined", "undetermined"): "不自动扩大，也不追加样本；两个问题都按“未定”写；是否另做实验由作者书面决定",
    ("undetermined", "negative"): "不自动扩大；报告：自动收取评分下收益为负（效果随评分规则反转），交互占比未定；后续由作者书面决定，反转不自动排除后续研究",
}


def next_step(ra, q1, q2, fuse):
    if fuse:
        return {"row": ["fuse"], "next_step": R_E["fuse"]}
    if ra != "pass":
        return {"row": [ra], "next_step": R_E[(ra,)]}
    return {"row": [ra, q1, q2], "next_step": R_E[(q1, q2)]}
