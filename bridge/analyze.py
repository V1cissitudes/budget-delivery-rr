"""判读入口（草稿 v4，未封存；候选阈值等作者拍板）。读 batch.py 写出的每槽 result.json，调用 analysis_rules.decide
（预登记 v4 §6）得出 R-a、Q1（交付交互是否过半）、Q2（自动收取后的残余收益）、R-c、R-d 和 R-e，另附：
- 跨产物口径的稳健性（只报告）：Q2 有确定方向时，用 W（每条轨迹一律取截止工作区）检查 Δ(BATS−Silent) 是否同向；
  W 的未知值单独按最不利方向填补（不套用 S ≤ H）。Number−Silent 的 ΔH、ΔW 另报。
- 严格版（只报告，低功效）、反转与“有害”的措辞义务（flags）、每个分支允许的措辞（claims）。
- 描述：各臂未知率（S/H 与 W 分开）、终止原因、SUB_EMPTY、CAP_COST、FORMAT_EXIT、资源中止、计价失败、未运行槽位、
  盲交率（启发式）、各差值的包络区间（共用重抽样的边际区间）、模型版本串（变化时按版本分层描述）、漂移检查（可选）。
没有 result.json 的槽位（没跑、支出停止或计价暂停后未完成）一律记未知；分母始终是 100 题 × 各臂。
用法：python3 -I bridge/analyze.py --results OUT --selection docs/prereg/2026-10-18_bridge_selection_DRAFT.json
        --expect-bridge-code SHA [--arms silent,neutral,number,bats] [--B 10000] [--out report.json]
        [--public-entry data/d1_census/raw/20260217_mini-v2.0.0_gemini-3-flash-high.json]"""
import argparse, json, os, sys
from collections import Counter
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402
import analysis_rules as AR  # noqa: E402
import prompts as P  # noqa: E402
from outcome import CODES  # noqa: E402

ANALYZE_VERSION = "bridge-analyze-v4.1"

# Allowed wording per branch (prereg v4 §6.4). analyze attaches the matching sentences; nothing else may be claimed.
CLAIMS = {
    "R-a": {"pass": "在本配置下，告知预算（BATS 式提示）在原生交付评分（S）下的收益通过了预设的大效应筛查判据（点估计 ≥ 门槛且 95% 区间下限 > 0）；不能据此说真实收益 ≥ 门槛，也不等于复现 18.6pp",
            "below": "告知收益低于大效应筛查门槛（区间上限 < 门槛）；不等于“没有效应”",
            "undetermined": "告知收益相对筛查门槛未定"},
    "Q1": {"over_half": "交付规则交互（J = T − Δ_H）超过总收益的一半；这是具体交付政策之间的收益差，不是自然中介比例，也不涉及规划机制",
           "under_half": "交付规则交互不到总收益的一半；这不等于“交付影响很小”",
           "undetermined": "交付规则交互占总收益的比例未定",
           "not_evaluated": "R-a 未达门槛，未评估交付交互"},
    "Q2": {"positive": "在自动收取评分（H）下，告知仍有正的残余收益；这不等于“交付影响很小”，也不识别规划改善",
           "negative": "在自动收取评分（H）下告知收益为负：效果随评分规则反转（原生交付评分 S 下为正、自动收取评分 H 下为负），两个方向都要报告",
           "undetermined": "自动收取之后的残余收益未定",
           "not_evaluated": "R-a 未达门槛，未评估残余收益"},
    "R-c": {"pass": "（次要）在共享计数块之上，策略段的额外作用通过大效应筛查判据（点估计 ≥ 门槛且区间下限 > 0）",
            "below": "（次要）策略段的额外作用低于大效应门槛（区间上限 < 门槛）；不等于没有作用",
            "undetermined": "（次要）策略段的额外作用相对门槛未定"},
    "R-d": {"pass": "（次要）预算数字与说明本身的作用通过大效应筛查判据（相对固定文本的 Neutral 臂）",
            "below": "（次要）预算数字与说明本身的作用低于大效应门槛；不等于没有作用",
            "undetermined": "（次要）预算数字与说明本身的作用相对门槛未定"},
    "W_robustness": {"both_products": "残余效应的方向在“统一取截止工作区（W）”的口径下同样成立（跨产物口径稳健性，不是机制证据，也不排除停止时点的作用）",
                     "H_only": "残余效应的方向只在混合产物口径（H）下达到判据，统一工作区口径（W）下未达到（未达到不等于 W 下没有效应）",
                     "undetermined": "W 的未知率超过 10%，跨口径稳健性未定",
                     "not_applicable": "Q2 未得出确定方向，不做跨口径稳健性检查"},
}


def load_selection(path):
    sel = json.load(open(path))
    if sel.get("stratum_sizes") != {"calls_gt_50": AR.POP["hi"], "calls_le_50": AR.POP["lo"]}:
        raise SystemExit(f"selection stratum sizes {sel.get('stratum_sizes')} != analysis_rules.POP {AR.POP}")
    strata = {i: "hi" for i in sel["high_60"]}
    strata.update({i: "lo" for i in sel["low_40"]})
    if len(strata) != len(sel["high_60"]) + len(sel["low_40"]):
        raise SystemExit("duplicate ids in selection")
    return strata


def load_results(results_dir, strata, arms, expect_bridge, expect_d1=ID.D1_EXPECTED):
    """{(iid, arm): result or None}. Every existing record must be terminal and carry the sealed identities;
    any mismatch stops the analysis (fail-closed) instead of silently dropping the record."""
    recs = {}
    for iid in strata:
        for arm in arms:
            p = os.path.join(results_dir, iid, arm, "result.json")
            if not os.path.exists(p):
                recs[(iid, arm)] = None
                continue
            r = json.load(open(p))
            i = r.get("identity") or {}
            bad = [k for k, v in (("bridge_code_sha256", expect_bridge), ("d1_code_sha256", expect_d1), ("k", ID.K),
                                  ("instance_id", iid), ("arm", arm), ("dev", False)) if i.get(k) != v]
            if not r.get("terminal") or bad or r.get("instance_id") != iid or r.get("arm") != arm:
                raise SystemExit(f"result identity mismatch at {p}: fields {bad}, terminal={r.get('terminal')}")
            recs[(iid, arm)] = r
    return recs


def load_results_multi(pairs, strata, arms, expect_d1=ID.D1_EXPECTED):
    """Addendum 2 (I2): results spread over several output dirs, each with ITS OWN code identity, e.g. after a
    pre-registered mid-run code change: [(dir, bridge_sha), ...] in run order. Every record is validated against its
    own dir's identity (fail-closed); for a slot present in several dirs the LAST dir wins. Returns (recs, source)."""
    recs, source = {(i, a): None for i in strata for a in arms}, {}
    for d, sha in pairs:
        part = load_results(d, strata, arms, sha, expect_d1)
        for key, r in part.items():
            if r is not None:
                recs[key] = r; source[f"{key[0]}/{key[1]}"] = os.path.abspath(d)
    return recs, source


def build_b1(recs, strata, arms):
    """F-b1 sensitivity data (prereg v4 §9): H and W replaced by H_b1 / W_b1 where present (else the F-v6 values)."""
    data, wdata = {}, {}
    for iid, st in strata.items():
        data[iid], wdata[iid] = {"stratum": st}, {"stratum": st}
        for arm in arms:
            r = recs[(iid, arm)] or {}
            h = r.get("H_b1", r.get("H")); w = r.get("W_b1", r.get("W"))
            data[iid][arm] = (r.get("S"), h); wdata[iid][arm] = (w, w)
    return data, wdata


def build(recs, strata, arms):
    """data: (S, H) per arm (analysis_rules data model); wdata: (W, W) per arm so analysis_rules' fills and bootstrap
    apply to W unchanged (an unknown W may be filled with 0 or 1)."""
    data, wdata = {}, {}
    for iid, st in strata.items():
        data[iid], wdata[iid] = {"stratum": st}, {"stratum": st}
        for arm in arms:
            r = recs[(iid, arm)] or {}
            data[iid][arm] = (r.get("S"), r.get("H"))
            wdata[iid][arm] = (r.get("W"), r.get("W"))
    return data, wdata


def point_range(d, coef):
    """[min, max] of the point estimate over all feasible fills of unknowns."""
    return (AR.objective(list(AR.fill(d, coef, "min").values()), coef),
            AR.objective(list(AR.fill(d, coef, "max").values()), coef))


def envelope(boot, d, a, b, which):
    """Δ(a−b) of S (which=0) or H/W (which=1): point range over fills, L under the min fill, U under the max fill
    (95% percentile, marginal)."""
    c = AR.diff(a, b, which)
    L, pmin, _ = AR.lower(boot, d, c); U, pmax, _ = AR.upper(boot, d, c)
    return {"point_min": pmin, "point_max": pmax, "L": L, "U": U}


def descriptives(recs, data, wdata, strata, arms):
    out = {}
    for arm in arms:
        rs = [recs[(i, arm)] for i in strata]
        codes = Counter((r or {}).get("code", "MISSING") for r in rs)
        m = {}
        for q, d, which in (("S", data, 0), ("H", data, 1), ("W", wdata, 1)):
            c = {arm: (1, 0) if which == 0 else (0, 1)}
            lo, hi = point_range(d, c)
            m[q] = {"weighted_mean_range": [lo, hi],
                    "unknown": sum(1 for i in strata if d[i][arm][which] is None),
                    "by_stratum_complete_case": {st: _cc([d[i][arm][which] for i in strata if strata[i] == st])
                                                 for st in AR.POP}}
        runs = [((r or {}).get("run") or {}) for r in rs]
        blind = [x.get("blind_submit") for x in runs if x.get("blind_submit") is not None]
        out[arm] = {"n_slots": len(rs), "codes": {k: codes.get(k, 0) for k in (*CODES, "MISSING")},
                    "sub_empty_rate": codes.get("SUB_EMPTY", 0) / len(rs),
                    "cap_cost": codes.get("CAP_COST", 0), "format_exit": codes.get("FORMAT_EXIT", 0),
                    "resource_stop": sum(1 for r in rs if (r or {}).get("resource_stop")),
                    "pricing_failure": sum(1 for r in rs if (r or {}).get("pricing_failure")),
                    # review C1 note: a capped trajectory whose F was unavailable is classified CAP_STEP_DIFF by
                    # convention, but there is no evidence that it had a diff; count these separately
                    "cap_with_F_unavailable": sum(1 for r in rs if (r or {}).get("code") in ("CAP_STEP_DIFF", "CAP_COST", "FORMAT_EXIT")
                                                  and not (((r or {}).get("run") or {}).get("F") or {}).get("available")),
                    "not_run": codes.get("MISSING", 0),
                    "blind_submit": {"n_submitted_with_rule": len(blind), "n_blind": sum(1 for x in blind if x),
                                     "rule": "启发式描述指标（bridge/outcome.blind_submit），不进判读"},
                    "unknown_rate_S_or_H": AR.unknown_rate(data, arm),
                    "unknown_rate_W": AR.unknown_rate(wdata, arm), **m}
    return out


def _cc(xs):
    k = [x for x in xs if x is not None]
    return {"n": len(k), "mean": (sum(k) / len(k)) if k else None, "unknown": len(xs) - len(k)}


def versions(recs, strata, arms):
    """Model version strings returned by the API (response.model) and, when captured, the raw modelVersion. If more
    than one appears, report S/H/W per version stratum (trajectories served by more than one version form their own
    'mixed' stratum). Descriptive only: the §6 rules are judged on the full sample (prereg v4 §3)."""
    per = {}
    allv, raw = set(), set()
    for (iid, arm), r in recs.items():
        if r is None:
            continue
        vs = tuple((r.get("identity") or {}).get("model_versions") or [])
        raw.update((r.get("identity") or {}).get("raw_model_versions") or [])
        allv.update(vs)
        key = vs[0] if len(vs) == 1 else ("mixed:" + "|".join(map(str, vs)) if vs else "none")
        per.setdefault(key, {}).setdefault(arm, []).append(r)
    rep = {"distinct": sorted(map(str, allv)), "raw_distinct": sorted(map(str, raw)), "changed": len(allv) > 1 or len(raw) > 1,
           "by_version": {}}
    if rep["changed"]:
        for key, by_arm in per.items():
            rep["by_version"][key] = {arm: {q: _cc([r.get(q) for r in rs]) for q in ("S", "H", "W")}
                                      for arm, rs in by_arm.items()}
    else:
        rep["by_version"] = {key: {arm: len(rs) for arm, rs in by_arm.items()} for key, by_arm in per.items()}
    return rep


def drift_check(data, strata, public_path, k=50):
    """Report-only (prereg v4 §4): Silent S vs synthetic A of the public uncapped entry truncated at k
    (A = resolved and api_calls <= k). Per-task agreement table on tasks where Silent S is known."""
    pub = json.load(open(public_path))
    A = {i: int(bool(pub[i]["resolved"]) and pub[i]["api_calls"] <= k) for i in strata if i in pub}
    tab = Counter()
    for i in strata:
        s = data[i]["silent"][0]
        if i in A and s is not None:
            tab[f"A{A[i]}_S{s}"] += 1
    n = sum(tab.values())
    return {"source": os.path.basename(public_path), "k": k, "n_tasks_compared": n, "missing_in_public": len(strata) - len(A),
            "table": dict(tab), "agree": (tab["A0_S0"] + tab["A1_S1"]) / n if n else None,
            "A_weighted": AR.wmean([{"stratum": strata[i], "a": A[i]} for i in strata if i in A], lambda r: r["a"])
            if len(A) == len(strata) else None, "affects_rules": False}


def registry_check(recs):
    """Review item 7: the pricing registry is outside the code identity, so check across all trajectories that it was
    only ever extended (a version's prices, the tolerance and the provisional amount never change) and that every
    formal trajectory ran under a confirmed registry. Report-only; any problem is flagged, nothing is dropped."""
    seen, glob, problems = {}, {}, []
    for (iid, arm), r in recs.items():
        reg = ((r or {}).get("identity") or {}).get("pricing_registry")
        if r is None or reg is None:
            continue
        if not reg.get("confirmed_by_author"):
            problems.append(f"{iid}/{arm}: registry not confirmed")
        for k in ("relative_tolerance", "provisional_usd_per_unpriced_call"):
            if glob.setdefault(k, reg.get(k)) != reg.get(k):
                problems.append(f"{iid}/{arm}: {k} changed")
        for v, e in (reg.get("versions") or {}).items():
            px = (e.get("input_per_token"), e.get("output_per_token"), e.get("cache_read_per_token"))
            if seen.setdefault(v, px) != px:
                problems.append(f"{iid}/{arm}: price of {v} changed")
    return {"consistent": not problems, "problems": problems[:50], "n_problems": len(problems),
            "versions_registered": sorted(seen)}


def claims(rep):
    out = []
    for k in ("R-a", "Q1", "Q2", "W_robustness", "R-c", "R-d"):
        v = rep.get(k)
        if v in CLAIMS[k]:
            out.append(f"{k}: {CLAIMS[k][v]}")
    return out


def analyze(recs, strata, arms, B=10000, public_path=None):
    data, wdata = build(recs, strata, arms)
    boot = AR.Boot(data, B)
    dec = AR.decide(data, wdata, B, boot=boot)
    rep = {"version": ANALYZE_VERSION, "rules_version": AR.RULES_VERSION, "B": B, "arms": arms,
           "unknown_rate": dec["unknown_rate"], "W_unknown_rate": dec["W_unknown_rate"],
           "unknown_cap_triggered": dec["fuse"]}
    for k in ("R-a", "Q1", "Q2", "R-c", "R-d", "W_robustness", "strict_holds", "flags", "R-e"):
        if k in dec:
            rep[k] = dec[k]
    rep["detail"] = dec.get("detail")
    if dec["fuse"]:
        rep["conservative_appendix"] = dec["conservative_appendix"]
    rep["claims"] = claims(rep) if not dec["fuse"] else ["未知率熔断：不作任何科学结论"]
    rep["note_on_delta_H"] = "Δ_H 显著为负只按“自动收取评分下收益为负”报告；不单独触发扩大实验，也不自动排除后续研究"
    pairs = [("bats", "silent"), ("bats", "number"), ("number", "silent")]
    if "neutral" in arms:
        pairs += [("number", "neutral"), ("neutral", "silent")]
    rep["diffs"] = {}
    for a, b in pairs:
        if a in arms and b in arms:
            rep["diffs"][f"{a}-{b}"] = {"dS": envelope(boot, data, a, b, 0), "dH": envelope(boot, data, a, b, 1),
                                        "dW": envelope(boot, wdata, a, b, 1)}
    rep["dW_main_pair"] = "bats-silent"
    rep["descriptives"] = descriptives(recs, data, wdata, strata, arms)
    rep["model_versions"] = versions(recs, strata, arms)
    d_b1, w_b1 = build_b1(recs, strata, arms)
    dec_b1 = AR.decide(d_b1, w_b1, B, boot=AR.Boot(d_b1, B))
    changed = sum(1 for r in recs.values() if r and r.get("F_b1_used"))
    rep["F_b1_sensitivity"] = {
        "label": "次要敏感性分析：H、W 改用按 F-b1 规则提取的截止产物（终止残留进程后重取、去掉新增的非 UTF-8 文件）；"
                 "F-b1 改变了提取和停止处理规则，不是恢复了原始截止状态；主判读以 F-v6 为准",
        "n_slots_using_F_b1": changed, "unknown_rate": dec_b1["unknown_rate"], "W_unknown_rate": dec_b1["W_unknown_rate"],
        "fuse": dec_b1["fuse"], **{k: dec_b1.get(k) for k in ("R-a", "Q1", "Q2", "R-c", "R-d", "W_robustness", "R-e")},
        "differs_from_primary": [k for k in ("R-a", "Q1", "Q2", "R-c", "R-d") if dec_b1.get(k) != dec.get(k)]}
    rep["pricing_registry_check"] = registry_check(recs)
    if not rep["pricing_registry_check"]["consistent"]:
        rep.setdefault("flags", []).append("REGISTRY_INCONSISTENT: 价目登记表在运行中被改动或未确认，见 pricing_registry_check")
    if public_path and "silent" in arms:
        rep["drift_check"] = drift_check(data, strata, public_path)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, action="append",
                    help="output dir; give DIR=BRIDGE_SHA (repeatable, in run order) when several identities are authorised")
    ap.add_argument("--selection", required=True)
    ap.add_argument("--expect-bridge-code", default=None, help="identity for a single plain --results DIR")
    ap.add_argument("--arms", default=",".join(P.ARMS)); ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--public-entry", default=None, help="public per-task file (resolved, api_calls) for the drift check")
    a = ap.parse_args()
    ID.check_d1()
    arms = [x for x in a.arms.split(",") if x]
    strata = load_selection(a.selection)
    pairs = []
    for x in a.results:
        d, _, sha = x.partition("=")
        sha = sha or a.expect_bridge_code
        if not sha:
            raise SystemExit(f"no identity for {d}: use DIR=BRIDGE_SHA or --expect-bridge-code")
        pairs.append((d, sha))
    recs, source = load_results_multi(pairs, strata, arms)
    rep = analyze(recs, strata, arms, a.B, a.public_entry)
    rep["inputs"] = {"results": [[os.path.abspath(d), sha] for d, sha in pairs], "selection": os.path.abspath(a.selection),
                     "d1_code_sha256": ID.D1_EXPECTED,
                     "slots_by_dir": {d: sum(1 for v in source.values() if v == os.path.abspath(d)) for d, _ in pairs}}
    if a.out:
        ID.atomic_json(a.out, rep)
    print(json.dumps({k: rep[k] for k in ("R-a", "Q1", "Q2", "R-c", "R-d", "W_robustness", "R-e", "flags") if k in rep},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
