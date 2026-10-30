"""Sampling SE vs fill range in the prior study, and the task-level joint distribution used to check the simulator.
For each functional and each of its two worst-case fills, the bootstrap SD of the stratum-weighted estimate (B = 10,000,
paired over tasks) is the sampling SE under that fill; the fill range is reported separately.
Run: cd next-paper && python3 -I analysis/bridge_rr/pilot_se.py"""
import sys, os, json, statistics as st
sys.path.insert(0, os.path.abspath("bridge")); sys.path.insert(0, os.path.abspath("analysis/bridge_rr"))
import analyze as AZ, analysis_rules as AR, tied_fill as TF
rep = json.load(open("data/bridge_formal_analysis/primary_report.json"))
pairs = [(d, sha) for d, sha in rep["inputs"]["results"]]
strata = AZ.load_selection(rep["inputs"]["selection"]); arms = ["silent", "bats"]
recs, _ = AZ.load_results_multi(pairs, strata, arms); data, _ = AZ.build(recs, strata, arms)
tied = {(i, a) for (i, a), r in recs.items() if a in arms and r.get("code") == "SUB_NONEMPTY" and r.get("S") is None}
TF.install(tied)
boot = AR.Boot(data, 10000)
C = {"G": {"bats": (1, -1), "silent": (-1, 1)}, "T": AR.diff("bats", "silent", 0), "dH": AR.diff("bats", "silent", 1),
     "F=0.5T-dH": {"bats": (0.5, -1), "silent": (-0.5, 1)}}
out = {}
for name, c in C.items():
    row = {}
    for sense in ("min", "max"):
        filled = AR.fill(data, c, sense); vals = boot.values(filled, c)
        # bootstrap distribution of the weighted mean
        import numpy as np
        acc = np.zeros(boot.B)
        for stv, ids in boot.ids.items():
            v = np.array([sum(cc[0] * filled[t][a][0] + cc[1] * filled[t][a][1] for a, cc in c.items()) for t in ids])
            acc += AR.POP[stv] / sum(AR.POP.values()) * v[boot.idx[stv]].mean(1)
        row[sense] = {"point": float(AR.wmean([{"stratum": filled[t]["stratum"], "v": sum(cc[0]*filled[t][a][0]+cc[1]*filled[t][a][1] for a, cc in c.items())} for t in filled], lambda r: r["v"])),
                      "boot_sd": float(acc.std(ddof=1))}
    out[name] = row
    print(f"{name}: min-fill point {100*row['min']['point']:.1f} SE {100*row['min']['boot_sd']:.2f} | max-fill point {100*row['max']['point']:.1f} SE {100*row['max']['boot_sd']:.2f} | fill range {100*(row['max']['point']-row['min']['point']):.1f}")
# task-level joint distribution (complete cases), per arm and discordance
def rate(arm, k):
    xs = [data[t][arm][k] for t in data if data[t][arm][k] is not None]; return sum(xs) / len(xs), len(xs)
desc = {}
for a in arms:
    s, ns = rate(a, 0); h, nh = rate(a, 1)
    resc = [data[t][a][1] - data[t][a][0] for t in data if None not in data[t][a]]
    desc[a] = {"S": s, "H": h, "rescue": sum(resc) / len(resc), "n_complete": len(resc)}
dis = {}
for k, lab in ((0, "S"), (1, "H")):
    both = [t for t in data if data[t]["bats"][k] is not None and data[t]["silent"][k] is not None]
    d = [(data[t]["bats"][k], data[t]["silent"][k]) for t in both]
    dis[lab] = {"n": len(d), "bats_only": sum(1 for b, s in d if b > s), "silent_only": sum(1 for b, s in d if s > b),
                "both1": sum(1 for b, s in d if b == s == 1)}
out["descriptives_complete_case_unweighted"] = desc; out["discordance"] = dis
print(json.dumps(desc, indent=1)); print(json.dumps(dis, indent=1))
json.dump(out, open("analysis/bridge_rr/pilot_se.json", "w"), indent=1)
