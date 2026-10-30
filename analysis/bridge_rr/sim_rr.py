"""Operating characteristics of the registered (Stage 1, v2) rules at n = 394 (strata 226 / 168), two arms.
v2 (2026-10-31, after the full-text review): n = 394 (the five development tasks removed: 2 hi, 3 lo); a submitted
trajectory with unknown verdict is filled with S = H (tied_fill); H2b is evaluated only when L_97.5(T) > 0; new boundary
scenario xvii_boundary_G0 (T ~ 18, dH = T, G = 0); --unk selects unknown patterns.
Generator: the v3/v4 review's ordinal latent model (shared task difficulty; fail < rescued < submitted-success; S <= H),
with MCAR and MNAR unknown patterns, as in docs/reviews/2026-10-19_bridge_v3_review_sim/v4/sim_v4.py, generalised to n.
Rules: sealed bridge/analysis_rules.py primitives (fill / lower / upper / screen / q1 / signed_claim), called directly;
G rule added here (L_97.5(G) > 0 'reduction', U_97.5(G) < 0 'reverse'). No gating: every label computed every rep.
Usage: python3 -I analysis/bridge_rr/sim_rr.py REPS B SEED [--only a,b] > log ; writes sim_rr_<REPS>_<B>_<SEED>.json"""
import sys, os, json, math, argparse, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import analysis_rules as AR  # noqa: E402
sys.path.insert(0, HERE); import tied_fill as TF  # noqa: E402
assert AR.RULES_VERSION.startswith("bridge-rules-v4"), AR.__file__
import importlib.util  # noqa: E402
SIMDIR = os.path.join(ROOT, "docs", "reviews", "2026-10-19_bridge_v3_review_sim")
_s = importlib.util.spec_from_file_location("sim3", os.path.join(SIMDIR, "sim.py")); S3 = importlib.util.module_from_spec(_s)
sys.modules["analysis_rules"] = AR; _s.loader.exec_module(S3)
_v = importlib.util.spec_from_file_location("sim4", os.path.join(SIMDIR, "v4", "sim_v4.py")); S4 = importlib.util.module_from_spec(_v)
_v.loader.exec_module(S4)

N = {"hi": 226, "lo": 168}                       # confirmatory sample: 499-task population minus 100 prior minus 5 dev (288-60-2, 211-40-3)
W = {st: AR.POP[st] / sum(AR.POP.values()) for st in AR.POP}   # population weights, as the sealed rules use
ARMS = ("silent", "bats")
CG = {"bats": (1, -1), "silent": (-1, 1)}        # G = dS - dH


class NpBoot:
    def __init__(self, data, B, rng):
        self.ids = {st: sorted(t for t, r in data.items() if r["stratum"] == st) for st in N}
        self.idx = {st: rng.integers(0, len(self.ids[st]), size=(B, len(self.ids[st]))) for st in self.ids}
        self.B = B
    def ci(self, filled, coef, alpha=AR.ALPHA):
        acc = np.zeros(self.B)
        for st, ids in self.ids.items():
            v = np.array([sum(c[0] * filled[t][a][0] + c[1] * filled[t][a][1] for a, c in coef.items()) for t in ids])
            acc += W[st] * v[self.idx[st]].mean(1)
        acc.sort(); B = self.B
        return acc[int(alpha / 2 * B)], acc[int((1 - alpha / 2) * B) - 1]


def gen(sc, rho, unk_spec, rng):
    n = sum(N.values()); sl = {"hi": slice(0, N["hi"]), "lo": slice(N["hi"], n)}
    u = rng.standard_normal(n); S, H = {}, {}
    for a in ARMS:
        z = math.sqrt(rho) * u + math.sqrt(1 - rho) * rng.standard_normal(n)
        s = np.zeros(n, int); h = np.zeros(n, int)
        for st in N:
            c1, c2 = S3.cut(*sc[a][st]); s[sl[st]] = z[sl[st]] >= c2; h[sl[st]] = z[sl[st]] >= c1
        S[a], H[a] = s, h
    kind, rates = unk_spec; data = {}; tied = set()
    for i in range(n):
        st = "hi" if i < N["hi"] else "lo"; data[f"t{i}"] = {"stratum": st}
        for a in ARMS:
            s, h = int(S[a][i]), int(H[a][i]); r = rates.get(a, rates.get("*", 0.0)); u_ = None
            if kind == "mcar" and rng.random() < r:
                k = rng.random(); u_ = "H" if k < 0.4 else ("S" if k < 0.6 else "ENG")
                if u_ == "S" and s == h: u_ = "TIED"      # submitted, judge unknown: H ≡ S, filled (0,0) or (1,1)
            elif kind == "capped" and s == 0 and rng.random() < r:
                u_ = "H"
            elif kind == "capped_salvaged" and s == 0 and h == 1 and rng.random() < r:
                u_ = "H"
            if u_ == "H": h = None
            elif u_ == "S": s = None
            elif u_ == "ENG": s = h = None
            elif u_ == "TIED": s = h = None; tied.add((f"t{i}", a))
            data[f"t{i}"][a] = (s, h)
    TF.install(tied)
    return data


def labels(data, boot):
    ur = {a: AR.unknown_rate(data, a) for a in ARMS}
    fuse = any(v > AR.MAX_UNKNOWN for v in ur.values())
    h1 = AR.screen(boot, data, "bats", "silent")["result"]
    L, _, _ = AR.lower(boot, data, CG, AR.ALPHA_Q); U, _, _ = AR.upper(boot, data, CG, AR.ALPHA_Q)
    h2 = "reduction" if L > 0 else ("reverse" if U < 0 else "undetermined")
    LT, _, _ = AR.lower(boot, data, AR.diff("bats", "silent", 0), AR.ALPHA_Q)
    h2b = AR.q1_interaction_share(boot, data)["result"] if LT > 0 else "not_evaluated"   # v2: needs a positive gain
    h3 = AR.signed_claim(boot, data, "bats", "silent", AR.ALPHA_Q)["result"]
    if fuse:
        h1 = h2 = h2b = h3 = "fuse"
    return dict(H1=h1, H2=h2, H2b=h2b, H3=h3, max_unknown=max(ur.values()))


SCEN = ["i_T18_viaDelivery", "ii_T18_beyondDelivery", "iii_null", "vi_T12_J0.4T", "ix_both_T18_J0.6T",
        "x_reversal_T18_dHneg", "xiii_boundary_J0.5T", "xiv_boundary_dH0", "iv_T8_harmH", "xvii_boundary_G0"]


def scen_all():
    s = S4.scen()
    SIL = S3.SIL   # boundary of H2: bats salvage rate equal to silent's in each stratum => J = G = 0, dH = T
    s["xvii_boundary_G0"] = dict(silent=SIL, neutral=SIL, number={"hi": (0.38, SIL["hi"][1]), "lo": (0.745, SIL["lo"][1])},
                                 bats={"hi": (0.38, SIL["hi"][1]), "lo": (0.745, SIL["lo"][1])})
    return s
UNK = {k: S4.UNK[k] for k in ("u0", "mcar5", "mcar8", "capped_bats8_sil2", "salvaged_bats15")}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("reps", type=int); ap.add_argument("B", type=int); ap.add_argument("seed", type=int)
    ap.add_argument("--only", default=""); ap.add_argument("--unk", default=""); ap.add_argument("--rho", type=float, default=0.85)
    a = ap.parse_args(); rng = np.random.default_rng(a.seed)
    allsc = scen_all(); names = [s for s in SCEN if (not a.only or s in a.only.split(","))]
    unks = {k: v for k, v in UNK.items() if (not a.unk or k in a.unk.split(","))}
    res = {"version": "sim_rr v2 (n=394, tied fill, H2b needs L(T)>0)", "n": N, "reps": a.reps, "B": a.B, "seed": a.seed, "rho": a.rho, "cells": {}}
    t0 = time.time()
    for name in names:
        sc = {arm: allsc[name][arm] for arm in ARMS}; tr = S3.truths({**sc, "neutral": sc["silent"], "number": sc["bats"]})
        tr["G"] = tr["T"] - tr["dH"]
        for uk, spec in unks.items():
            cnt = {k: {} for k in ("H1", "H2", "H2b", "H3")}; mu = []
            for _ in range(a.reps):
                data = gen(sc, a.rho, spec, rng); lab = labels(data, NpBoot(data, a.B, rng)); mu.append(lab["max_unknown"])
                for k in cnt: cnt[k][lab[k]] = cnt[k].get(lab[k], 0) + 1
            cell = {"truth": {k: round(100 * v, 1) if isinstance(v, float) else v for k, v in tr.items()},
                    "rates_pct": {k: {lab: round(100 * c / a.reps, 1) for lab, c in v.items()} for k, v in cnt.items()},
                    "counts": cnt,
                    "mean_max_unknown_pct": round(100 * float(np.mean(mu)), 1)}
            res["cells"][f"{name}|{uk}"] = cell
            print(f"[{time.time()-t0:6.0f}s] {name} {uk} truth T={cell['truth']['T']} dH={cell['truth']['dH']} G={cell['truth']['G']} | "
                  f"H1 {cell['rates_pct']['H1']} | H2 {cell['rates_pct']['H2']} | H2b {cell['rates_pct']['H2b']} | H3 {cell['rates_pct']['H3']}", flush=True)
    out = os.path.join(HERE, f"sim_rr_v2_{a.reps}_{a.B}_{a.seed}.json"); json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    print("wrote", out)


if __name__ == "__main__":
    main()
