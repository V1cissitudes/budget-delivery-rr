"""Operating characteristics of the bridge prereg DRAFT v4 rules (bridge/analysis_rules.py, called directly).
Only the bootstrap engine is swapped for a numpy one with the same interface (Boot.ci(filled, coef, alpha)); decisions
are checked against the pure-Python engine first (validate()). Data generator: the v3 review's ordinal latent model
(../sim.py gen/scen), extended with W unknowns, v4 scenarios and MNAR unknowns (concentrated in capped trajectories,
different rates per arm).
Usage: python3 sim_v4.py REPS B SEED [--alpha-q 0.05|0.025] [--only name,name] > log
Outputs sim_v4_<REPS>_<B>_<SEED>_aq<alpha>.json. Monte Carlo SE of a rate p over REPS: sqrt(p(1-p)/REPS)."""
import sys, os, json, math, zlib, argparse
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "bridge"))   # bridge/analysis_rules.py (v4)
sys.path.insert(1, os.path.join(HERE, ".."))                               # ../sim.py (generator only)
import analysis_rules as AR  # noqa: E402
assert AR.RULES_VERSION.startswith("bridge-rules-v4"), AR.__file__
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("sim3", os.path.join(HERE, "..", "sim.py"))
S3 = importlib.util.module_from_spec(_spec)
sys.modules["analysis_rules"] = AR      # ../sim.py imports analysis_rules; give it v4 (it only uses fill/diff there)
_spec.loader.exec_module(S3)

W = {"hi": 288 / 499, "lo": 211 / 499}


class NpBoot:
    def __init__(self, data, B, rng):
        self.ids = {st: sorted(t for t, r in data.items() if r["stratum"] == st) for st in ("hi", "lo")}
        self.idx = {st: rng.integers(0, len(self.ids[st]), size=(B, len(self.ids[st]))) for st in self.ids}
        self.B = B

    def ci(self, filled, coef, alpha=0.05):
        acc = np.zeros(self.B)
        for st, ids in self.ids.items():
            v = np.array([sum(c[0] * filled[t][a][0] + c[1] * filled[t][a][1] for a, c in coef.items()) for t in ids])
            acc += W[st] * v[self.idx[st]].mean(1)
        acc.sort(); B = self.B
        return acc[int(alpha / 2 * B)], acc[int((1 - alpha / 2) * B) - 1]


def to_rows(S, H, Wv, unk):
    """unk[a][i] in {None, 'H', 'S', 'SH', 'ENG'}: H unknown also makes W unknown when the trajectory is unsubmitted
    (same F product); ENG makes S, H, W unknown; 'S' = judge unknown on the submission only."""
    data, wdata = {}, {}
    for i in range(100):
        st = "hi" if i < 60 else "lo"
        data[f"t{i}"] = {"stratum": st}; wdata[f"t{i}"] = {"stratum": st}
        for a in S:
            s, h, w = int(S[a][i]), int(H[a][i]), int(Wv[a][i])
            u = unk[a][i]
            if u == "H":
                h = None; w = None if s == 0 else w
            elif u == "S":
                s = None
            elif u in ("SH", "ENG"):
                s = h = w = None
            data[f"t{i}"][a] = (s, h); wdata[f"t{i}"][a] = (w, w)
    return data, wdata


def gen(sc, rho, unk_spec, rng, w_mismatch=0.05):
    arms = list(sc)
    u = rng.standard_normal(100)
    S, H, Wv = {}, {}, {}
    for a in arms:
        z = math.sqrt(rho) * u + math.sqrt(1 - rho) * rng.standard_normal(100)
        s = np.zeros(100, int); h = np.zeros(100, int)
        for st, sl in (("hi", slice(0, 60)), ("lo", slice(60, 100))):
            c1, c2 = S3.cut(*sc[a][st]); s[sl] = z[sl] >= c2; h[sl] = z[sl] >= c1
        S[a], H[a] = s, h
        flip = rng.random(100) < w_mismatch
        Wv[a] = np.where(s == 1, np.where(flip, 1 - s, s), h)
    unk = {a: [None] * 100 for a in arms}
    kind, rates = unk_spec
    for a in arms:
        r = rates.get(a, rates.get("*", 0.0))
        for i in range(100):
            if kind == "mcar" and rng.random() < r:
                k = rng.random(); unk[a][i] = "H" if k < 0.4 else ("S" if k < 0.6 else "ENG")
            elif kind == "capped" and S[a][i] == 0 and rng.random() < r:   # MNAR: only unsubmitted runs lose H (and W)
                unk[a][i] = "H"
            elif kind == "capped_salvaged" and S[a][i] == 0 and H[a][i] == 1 and rng.random() < r:  # worst MNAR
                unk[a][i] = "H"
    data, wdata = to_rows(S, H, Wv, unk)
    return data, wdata, S, H


SIL = S3.SIL
def scen():
    s = {k: v for k, v in S3.scen().items()}
    s["ix_both_T18_J0.6T"] = dict(silent=SIL, neutral=SIL, number={"hi": (0.38, 0.30), "lo": (0.745, 0.0)},
                                  bats={"hi": (0.38, 0.30), "lo": (0.745, 0.0)})
    s["x_reversal_T18_dHneg"] = dict(silent=SIL, neutral=SIL, number={"hi": (0.38, 0.05), "lo": (0.745, 0.0)},
                                     bats={"hi": (0.38, 0.05), "lo": (0.745, 0.0)})
    s["xi_opposite_Sdown_Hup"] = dict(silent=SIL, neutral=SIL, number={"hi": (0.04, 0.70), "lo": (0.62, 0.15)},
                                      bats={"hi": (0.04, 0.70), "lo": (0.62, 0.15)})
    big = {"hi": (0.70, 0.05), "lo": (0.80, 0.0)}       # T~39, ΔH~12, J/T~0.69: 'both' with a very large T
    s["xii_both_T39"] = dict(silent=SIL, neutral=SIL, number=big, bats=big)
    b05 = {"hi": (0.38, 0.358), "lo": (0.745, 0.0)}     # boundary of Q1: J = 0.5T exactly (T~18, ΔH~9)
    s["xiii_boundary_J0.5T"] = dict(silent=SIL, neutral=SIL, number=b05, bats=b05)
    b0 = {"hi": (0.38, 0.2017), "lo": (0.745, 0.0)}     # boundary of Q2: ΔH = 0 exactly (T~18)
    s["xiv_boundary_dH0"] = dict(silent=SIL, neutral=SIL, number=b0, bats=b0)
    # strata in opposite directions on H (author feedback 2026-10-19): hi ΔH −10, lo ΔH +13.6 => weighted ΔH = 0
    so0 = {"hi": (0.38, 0.12), "lo": (0.745, 0.111)}
    s["xv_strata_opposite_dH0"] = dict(silent=SIL, neutral=SIL, number=so0, bats=so0)
    so5 = {"hi": (0.38, 0.12), "lo": (0.745, 0.23)}      # hi ΔH −10, lo ΔH +25.5 => weighted ΔH ≈ +5
    s["xvi_strata_opposite_dH5"] = dict(silent=SIL, neutral=SIL, number=so5, bats=so5)
    return s


def truths(sc):
    t = S3.truths(sc); t["J/T"] = t["J"] / t["T"] if abs(t["T"]) > 1e-9 else None
    return t


UNK = {"u0": ("mcar", {"*": 0.0}), "mcar5": ("mcar", {"*": 0.05}), "mcar8": ("mcar", {"*": 0.08}),
       "capped_bats8_sil2": ("capped", {"bats": 0.08, "number": 0.08, "silent": 0.02, "neutral": 0.02}),
       "capped_sil8_bats2": ("capped", {"silent": 0.08, "neutral": 0.08, "bats": 0.02, "number": 0.02}),
       "salvaged_bats15": ("capped_salvaged", {"bats": 0.15, "number": 0.15})}


def one(sc, rho, unk, rng, B):
    data, wdata, S, H = gen(sc, rho, unk, rng)
    boot = NpBoot(data, B, rng)
    o = AR.decide(data, wdata, B, boot=boot)
    return o, max(o["unknown_rate"].values())


def validate(n=6, B=2000):
    rng = np.random.default_rng(11); agree = tot = 0
    for name, sc in list(scen().items())[:6]:
        for _ in range(n // 3 or 1):
            data, wdata, *_ = gen(sc, 0.75, UNK["mcar5"], rng)
            a = AR.decide(data, wdata, B)                       # pure-Python engine
            b = AR.decide(data, wdata, B, boot=NpBoot(data, B, rng))
            for k in ("R-a", "Q1", "Q2", "R-c", "R-d", "W_robustness"):
                tot += 1; agree += a[k] == b[k]
                if a[k] != b[k]:
                    print("VALIDATE DIFF", name, k, a[k], b[k], flush=True)
    print(f"validate: {agree}/{tot} decisions agree (different resamples, B={B})", flush=True)
    return agree, tot


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("reps", type=int); ap.add_argument("B", type=int); ap.add_argument("seed", type=int)
    ap.add_argument("--alpha-q", type=float, default=0.05); ap.add_argument("--sign-p", type=float, default=None); ap.add_argument("--only", default="")
    ap.add_argument("--rhos", default="0.85"); ap.add_argument("--unk", default=",".join(UNK)); ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()
    AR.ALPHA_Q = a.alpha_q
    if a.sign_p is not None:
        AR.SIGN_P = a.sign_p        # 1.01 disables the sign-test guard
    if a.validate:
        validate()
    res = {}
    only = set(x for x in a.only.split(",") if x)
    for name, sc in scen().items():
        if only and name not in only:
            continue
        for rho in map(float, a.rhos.split(",")):
            for uk in a.unk.split(","):
                rng = np.random.default_rng([a.seed, zlib.crc32(name.encode()) % 10**6, int(rho * 100), zlib.crc32(uk.encode()) % 10**6])
                cnt = {}
                for _ in range(a.reps):
                    o, umax = one(sc, rho, UNK[uk], rng, a.B)
                    for k in ("R-a", "Q1", "Q2", "R-c", "R-d", "W_robustness"):
                        cnt.setdefault(k, {}); cnt[k][o[k]] = cnt[k].get(o[k], 0) + 1
                    j = f'{o["Q1"]}|{o["Q2"]}'; cnt.setdefault("Q1|Q2", {}); cnt["Q1|Q2"][j] = cnt["Q1|Q2"].get(j, 0) + 1
                    cnt["fuse"] = cnt.get("fuse", 0) + int(o["fuse"])
                    cnt["strict"] = cnt.get("strict", 0) + int(bool(o.get("strict_holds")))
                    cnt["expand"] = cnt.get("expand", 0) + int(o["R-e"]["row"][1:2] == ["over_half"])
                    cnt["reversal_flag"] = cnt.get("reversal_flag", 0) + int(any(f.startswith("REVERSAL") for f in o["flags"]))
                key = f"{name}|rho{rho}|{uk}"
                res[key] = dict(truth=truths(sc), counts=cnt, reps=a.reps, B=a.B, alpha_q=a.alpha_q, unknown=UNK[uk])
                print(key, json.dumps(res[key]["counts"]), flush=True)
    out = os.path.join(HERE, f"sim_v4_{a.reps}_{a.B}_{a.seed}_aq{a.alpha_q}{'_sp' + str(a.sign_p) if a.sign_p is not None else ''}{'_' + a.only.replace(',', '+') if a.only else ''}.json")
    json.dump(res, open(out, "w"), indent=1, default=float)
    print("wrote", out)
