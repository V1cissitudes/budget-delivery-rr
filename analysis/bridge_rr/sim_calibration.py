"""Does the simulator's task-level joint distribution look like the prior study's? For the generator scenarios closest to
the prior study (i: mainly delivery; ix: both), at n = 100 (strata 60/40) and task correlation rho in {0.85, 0.60}:
per-arm S, H and rescue rates, discordant-pair counts on S and on H, and the bootstrap SE of G, T, dH (B = 2000),
averaged over 200 generated data sets. Compare with analysis/bridge_rr/pilot_se.json.
Run: cd next-paper && python3 -I analysis/bridge_rr/sim_calibration.py"""
import sys, os, json, math
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import sim_rr as SR
AR = SR.AR
SR.N.clear(); SR.N.update({"hi": 60, "lo": 40})
SR.W.clear(); SR.W.update({st: AR.POP[st] / sum(AR.POP.values()) for st in AR.POP})
allsc = SR.scen_all(); rng = np.random.default_rng(3)
C = {"G": SR.CG, "T": AR.diff("bats", "silent", 0), "dH": AR.diff("bats", "silent", 1)}
out = {}
for name in ("i_T18_viaDelivery", "ix_both_T18_J0.6T"):
    sc = {arm: allsc[name][arm] for arm in SR.ARMS}
    for rho in (0.85, 0.60):
        acc = {"S_sil": [], "S_bats": [], "resc_sil": [], "resc_bats": [], "discS": [], "discH": [], "se_G": [], "se_T": [], "se_dH": []}
        for _ in range(200):
            data = SR.gen(sc, rho, SR.UNK["u0"], rng); boot = SR.NpBoot(data, 2000, rng)
            ids = list(data)
            S = {a: np.array([data[t][a][0] for t in ids]) for a in SR.ARMS}; H = {a: np.array([data[t][a][1] for t in ids]) for a in SR.ARMS}
            acc["S_sil"].append(S["silent"].mean()); acc["S_bats"].append(S["bats"].mean())
            acc["resc_sil"].append((H["silent"] - S["silent"]).mean()); acc["resc_bats"].append((H["bats"] - S["bats"]).mean())
            acc["discS"].append(int((S["bats"] != S["silent"]).sum())); acc["discH"].append(int((H["bats"] != H["silent"]).sum()))
            for k, c in C.items():
                b = np.zeros(boot.B)
                for st, tids in boot.ids.items():
                    v = np.array([sum(cc[0] * data[t][a][0] + cc[1] * data[t][a][1] for a, cc in c.items()) for t in tids])
                    b += SR.W[st] * v[boot.idx[st]].mean(1)
                acc[f"se_{k}"].append(b.std(ddof=1))
        row = {k: float(np.mean(v)) for k, v in acc.items()}
        out[f"{name}|rho{rho}"] = row
        print(f"{name} rho={rho}: S {100*row['S_sil']:.0f}/{100*row['S_bats']:.0f}  rescue {100*row['resc_sil']:.0f}/{100*row['resc_bats']:.0f}  "
              f"discordant S {row['discS']:.0f} H {row['discH']:.0f} of 100 | SE at n=100: G {100*row['se_G']:.1f} T {100*row['se_T']:.1f} dH {100*row['se_dH']:.1f}")
json.dump(out, open(os.path.join(HERE, "sim_calibration.json"), "w"), indent=1)
