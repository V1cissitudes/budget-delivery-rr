"""Operating-characteristic simulation of bridge prereg v3 §4/§6 rules.
Uses analysis_rules.fill / feasible / objective / u_exact_salvage / unknown_rate directly; only the bootstrap is
re-implemented in numpy (same stratified task-paired percentile scheme, same index convention, common random numbers
across all calls within one dataset, as in AR.boot_ci with its fixed seed)."""
import sys, json, math, itertools, zlib
import numpy as np
from statistics import NormalDist
import analysis_rules as AR

ND = NormalDist()
W = {"hi": 288 / 499, "lo": 211 / 499}
NH = {"hi": 60, "lo": 40}
STRAT = np.array(["hi"] * 60 + ["lo"] * 40)
IDX_HI = np.arange(60); IDX_LO = np.arange(60, 100)


def make_boot(rng, B):
    return rng.integers(0, 60, size=(B, 60)), rng.integers(0, 40, size=(B, 40)) + 60


def vec(d, coef):
    v = np.zeros(100)
    for i, r in enumerate(d.values()):
        v[i] = sum(c[0] * r[a][0] + c[1] * r[a][1] for a, c in coef.items())
    return v


def boot_ci(d, coef, BI):
    v = vec(d, coef); bh, bl = BI
    vals = W["hi"] * v[bh].mean(1) + W["lo"] * v[bl].mean(1)
    vals.sort(); B = len(vals)
    return vals[int(0.025 * B)], vals[int(0.975 * B) - 1]


def point(d, coef):
    v = vec(d, coef)
    return W["hi"] * v[:60].mean() + W["lo"] * v[60:].mean()


def screen(data, a, b, BI):
    c = AR.diff(a, b, 0)
    dmin = AR.fill(data, c, "min"); lo, _ = boot_ci(dmin, c, BI)
    if point(dmin, c) >= AR.THRESH and lo > 0:
        return "pass"
    dmax = AR.fill(data, c, "max"); _, hi = boot_ci(dmax, c, BI)
    return "below" if hi < AR.THRESH else "undetermined"


def interval_minmax(data, coef, BI):
    """least-favourable lower bound (min fill) and upper bound (max fill), plus both point estimates"""
    dmin = AR.fill(data, coef, "min"); dmax = AR.fill(data, coef, "max")
    return boot_ci(dmin, coef, BI)[0], boot_ci(dmax, coef, BI)[1], point(dmin, coef), point(dmax, coef)


def decide(data, BI, W_data=None):
    arms = [a for a in data[next(iter(data))] if a != "stratum"]
    out = {}
    ur = {a: AR.unknown_rate(data, a) for a in arms}
    if any(v > AR.MAX_UNKNOWN for v in ur.values()):
        return {"R-a": "und_unk", "R-b": "und_unk", "R-b_alt": "und_unk", "R-c": "und_unk", "R-d": "und_unk",
                "strict": False, "gate_NS_agree": None, "gate_BS_agree": None, "dH_neg": False, "dH_pos": False}
    out["R-a"] = screen(data, "bats", "silent", BI)
    most = {"bats": (0.5, -1), "silent": (-0.5, 1)}
    few = {"bats": (0.75, -1), "silent": (-0.75, 1)}
    strictc = {"bats": (0.2, -1), "silent": (-0.2, 1)}           # J - 0.8T
    cT = AR.diff("bats", "silent", 0); cH = AR.diff("bats", "silent", 1)
    if out["R-a"] != "pass":
        out["R-b"] = out["R-b_alt"] = "not_eval"
    else:
        dmin = AR.fill(data, most, "min"); lo_most = boot_ci(dmin, most, BI)[0]
        ev = sum(h - s for s, h in (r["silent"] for r in dmin.values()))
        dmax = AR.fill(data, few, "max"); hi_few = boot_ci(dmax, few, BI)[1]
        ev_max = sum(h - s for s, h in (r["silent"] for r in dmax.values()))
        lT = boot_ci(AR.fill(data, cT, "min"), cT, BI)[0]
        ux = AR.u_exact_salvage(data)
        out["detail"] = dict(lo_most=lo_most, hi_few=hi_few, lT=lT, ux=ux, ev=ev)
        if lo_most > 0 and ev >= AR.MIN_EVENTS_MOST:
            out["R-b"] = "most"
        elif hi_few < 0 and ux < 0.25 * lT:
            out["R-b"] = "little"
        else:
            out["R-b"] = "undetermined"
        # alternative (review proposal): exact R0 bound only as a rare-event fallback, mirroring the 'most' rule
        if lo_most > 0 and ev >= AR.MIN_EVENTS_MOST:
            out["R-b_alt"] = "most"
        elif hi_few < 0 and (ev_max >= AR.MIN_EVENTS_MOST or ux < 0.25 * lT):
            out["R-b_alt"] = "little"
        else:
            out["R-b_alt"] = "undetermined"
    out["R-c"] = screen(data, "bats", "number", BI)
    out["R-d"] = screen(data, "number", "neutral", BI) if "neutral" in arms else None
    # strict secondary report: L(J-0.8T) > 0 and CI(dH) inside [-5, 5]pp (least-favourable fills)
    ls = interval_minmax(data, strictc, BI)[0]
    hl, hu, _, _ = interval_minmax(data, cH, BI)
    out["strict"] = bool(ls > 0 and hl > -0.05 and hu < 0.05)
    out["dH_neg"] = bool(hu < 0); out["dH_pos"] = bool(hl > 0)
    out["dH_ci_width"] = hu - hl
    # v3 dW gate: sign agreement of point estimates dH vs dW (W known fully here, unknowns in H filled by obs only)
    if W_data is not None:
        def pt(arr_a, arr_b):
            return W["hi"] * (arr_a[:60] - arr_b[:60]).mean() + W["lo"] * (arr_a[60:] - arr_b[60:]).mean()
        Hs = {a: np.array([r[a][1] if r[a][1] is not None else np.nan for r in data.values()]) for a in arms}
        def ptH(a, b):
            m = ~np.isnan(Hs[a]) & ~np.isnan(Hs[b])
            x = np.where(m, Hs[a] - Hs[b], 0.0)
            return W["hi"] * x[:60].sum() / max(m[:60].sum(), 1) + W["lo"] * x[60:].sum() / max(m[60:].sum(), 1)
        out["gate_NS_agree"] = bool(np.sign(ptH("number", "silent")) == np.sign(pt(W_data["number"], W_data["silent"])))
        out["gate_BS_agree"] = bool(np.sign(ptH("bats", "silent")) == np.sign(pt(W_data["bats"], W_data["silent"])))
    return out


# ---------- data generation ----------
def cut(p_succ, p_salv):
    p_fail = max(1e-9, 1 - p_succ - p_salv)
    return ND.inv_cdf(min(p_fail, 1 - 1e-9)), ND.inv_cdf(min(p_fail + p_salv, 1 - 1e-9))


def gen(scn, rho, urate, rng, w_mismatch=0.05):
    """scn[arm][stratum] = (P(S=1), P(S=0,H=1)). Shared task latent u -> ordinal outcome fail<salvage<success."""
    arms = list(scn)
    u = rng.standard_normal(100)
    S, H = {}, {}
    for a in arms:
        z = math.sqrt(rho) * u + math.sqrt(1 - rho) * rng.standard_normal(100)
        s = np.zeros(100, int); h = np.zeros(100, int)
        for st, sl in (("hi", slice(0, 60)), ("lo", slice(60, 100))):
            c1, c2 = cut(*scn[a][st])
            zz = z[sl]
            s[sl] = zz >= c2; h[sl] = zz >= c1
        S[a], H[a] = s, h
    # W: workspace at end of run. capped/unsubmitted -> W = H; submitted -> W = S except a small mismatch rate
    Wd = {}
    for a in arms:
        flip = rng.random(100) < w_mismatch
        Wd[a] = np.where(S[a] == 1, np.where(flip, 1 - S[a], S[a]), H[a])
    data = {}
    for i in range(100):
        row = {"stratum": STRAT[i]}
        for a in arms:
            s, h = int(S[a][i]), int(H[a][i])
            if rng.random() < urate:
                k = rng.random()
                if k < 0.4: h = None            # F/judge unknown on H only
                elif k < 0.6: s = None          # judge unknown on S only
                else: s = h = None              # ENG_FAIL twice
            row[a] = (s, h)
        data[f"t{i}"] = row
    return data, Wd, S, H


SIL = {"hi": (0.10, 0.50), "lo": (0.70, 0.02)}          # S_A ~35.4%, R0 ~29.7%
def scen():
    s = {}
    BATS_i = {"hi": (0.38, 0.17), "lo": (0.745, 0.005)}  # T~+18, R1~10 -> J~+20, dH~-2
    BATS_ii = {"hi": (0.38, 0.45), "lo": (0.745, 0.05)}  # T~+18, R1~28 -> J~+2, dH~+16
    BATS_iv = {"hi": (0.24, 0.12), "lo": (0.73, 0.0)}    # T~+8, R1~7 -> J~+23, dH~-15 (panic early submit)
    BATS_vi = {"hi": (0.30, 0.43), "lo": (0.72, 0.0)}    # T~+12, J~+5 (=0.4T), dH~+7  (borderline 'not most')
    BATS_vii = {"hi": (0.30, 0.12), "lo": (0.72, 0.0)}   # T~+12, R1~7, J~+23 (most, smaller T)
    SIL_rare = {"hi": (0.10, 0.03), "lo": (0.70, 0.0)}   # R0 ~1.7% : delivery cannot matter
    BATS_rare = {"hi": (0.38, 0.01), "lo": (0.745, 0.0)} # T~+18, J~+1
    s["i_T18_viaDelivery"] = dict(silent=SIL, neutral=SIL, number=BATS_i, bats=BATS_i)
    s["ii_T18_beyondDelivery"] = dict(silent=SIL, neutral=SIL, number=BATS_ii, bats=BATS_ii)
    s["iii_null"] = dict(silent=SIL, neutral=SIL, number=SIL, bats=SIL)
    s["iv_T8_harmH"] = dict(silent=SIL, neutral=SIL, number=BATS_iv, bats=BATS_iv)
    s["vi_T12_J0.4T"] = dict(silent=SIL, neutral=SIL, number=BATS_vi, bats=BATS_vi)
    s["vii_T12_viaDelivery"] = dict(silent=SIL, neutral=SIL, number=BATS_vii, bats=BATS_vii)
    s["v_rareSalvage_T18"] = dict(silent=SIL_rare, neutral=SIL_rare, number=BATS_rare, bats=BATS_rare)
    s["viii_G_only"] = dict(silent=SIL, neutral=SIL, number=SIL, bats=BATS_i)   # R-c true, R-d null
    return s


def truths(sc):
    def m(a, k):
        return sum(W[st] * (sc[a][st][0] if k == "S" else sc[a][st][0] + sc[a][st][1]) for st in W)
    T = m("bats", "S") - m("silent", "S"); dH = m("bats", "H") - m("silent", "H")
    R0 = m("silent", "H") - m("silent", "S"); R1 = m("bats", "H") - m("bats", "S")
    return dict(T=T, dH=dH, J=T - dH, R0=R0, R1=R1)


if __name__ == "__main__":
    reps = int(sys.argv[1]); B = int(sys.argv[2]); seed = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    rhos = {"rho0.85": 0.85, "rho0.60": 0.60}
    urates = [0.0, 0.05, 0.08]
    res = {}
    for name, sc in scen().items():
        for rk, rho in rhos.items():
            for ur in urates:
                rng = np.random.default_rng([seed, zlib.crc32(name.encode()) % 10**6, int(rho * 100), int(ur * 100)])
                cnt = {}; qs = []; widths = []
                for _ in range(reps):
                    data, Wd, S, H = gen(sc, rho, ur, rng)
                    qs.append(np.mean(S["bats"] != S["silent"]))
                    BI = make_boot(rng, B)
                    o = decide(data, BI, Wd)
                    for k in ("R-a", "R-b", "R-b_alt", "R-c", "R-d"):
                        cnt.setdefault(k, {}); cnt[k][o[k]] = cnt[k].get(o[k], 0) + 1
                    for k in ("strict", "dH_neg", "dH_pos"):
                        cnt.setdefault(k, 0); cnt[k] += int(o[k])
                    if o.get("gate_NS_agree") is not None:
                        cnt.setdefault("gateNS_disagree", 0); cnt["gateNS_disagree"] += int(not o["gate_NS_agree"])
                        cnt.setdefault("gateBS_disagree", 0); cnt["gateBS_disagree"] += int(not o["gate_BS_agree"])
                    if "dH_ci_width" in o: widths.append(o["dH_ci_width"])
                key = f"{name}|{rk}|u{ur}"
                res[key] = dict(truth=truths(sc), q_S=float(np.mean(qs)), dH_ci_width=float(np.mean(widths)) if widths else None, counts=cnt, reps=reps)
                print(key, json.dumps(res[key], default=float), flush=True)
    json.dump(res, open(f"sim_results_{reps}_{B}_{seed}.json", "w"), indent=1, default=float)
