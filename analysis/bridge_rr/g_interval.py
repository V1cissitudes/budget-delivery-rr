"""Pilot estimate of G = Δ_S − Δ_H (BATS − Silent) and of T, Δ_H, 0.5T−Δ_H, under (a) the sealed fill and (b) the
registered fill of the RR (tied_fill: a submitted trajectory with unknown verdict is filled with S = H).
Sealed bridge modules are imported unchanged. Run: cd next-paper && python3 -I analysis/bridge_rr/g_interval.py"""
import sys, os, json
sys.path.insert(0, os.path.abspath("bridge")); sys.path.insert(0, os.path.abspath("analysis/bridge_rr"))
import analyze as AZ, analysis_rules as AR, tied_fill as TF
rep = json.load(open("data/bridge_formal_analysis/primary_report.json"))
pairs = [(d, sha) for d, sha in rep["inputs"]["results"]]
strata = AZ.load_selection(rep["inputs"]["selection"])
arms = ["silent", "bats"]
recs, _ = AZ.load_results_multi(pairs, strata, arms)
data, _ = AZ.build(recs, strata, arms)
tied = {(i, a) for (i, a), r in recs.items() if a in arms and r.get("code") == "SUB_NONEMPTY" and r.get("S") is None}
print("submitted-with-unknown-verdict slots:", sorted(tied))
B = 10000
boot = AR.Boot(data, B)
cG = {"bats": (1, -1), "silent": (-1, 1)}          # G = (S_b − H_b) − (S_s − H_s) = R0 − R1
cT = AR.diff("bats", "silent", 0); cH = AR.diff("bats", "silent", 1)
cF = {"bats": (0.5, -1), "silent": (-0.5, 1)}      # 0.5T − Δ_H (H2b)
out = {"tied_slots": sorted(tied)}
for mode in ("sealed_fill", "tied_fill"):
    if mode == "tied_fill": TF.install(tied)
    out[mode] = {}
    for name, c in (("G=dS-dH", cG), ("T=dS", cT), ("dH", cH), ("0.5T-dH", cF)):
        L, pmin, _ = AR.lower(boot, data, c); U, pmax, _ = AR.upper(boot, data, c)
        L975, _, _ = AR.lower(boot, data, c, alpha=0.025); U975, _, _ = AR.upper(boot, data, c, alpha=0.025)
        out[mode][name] = {"point_min": pmin, "point_max": pmax, "L95": L, "U95": U, "L97.5": L975, "U97.5": U975}
        print(f"[{mode}] {name}: point {100*pmin:.1f}–{100*pmax:.1f} pp; 95% [{100*L:.1f}, {100*U:.1f}]; 97.5% [{100*L975:.1f}, {100*U975:.1f}]")
    TF.uninstall()
json.dump(out, open("analysis/bridge_rr/g_interval_pilot.json", "w"), indent=1)
