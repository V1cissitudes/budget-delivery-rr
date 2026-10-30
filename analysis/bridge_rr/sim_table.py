"""Turn sim_rr_<...>.json into the LaTeX rows for the Stage 1 report (§Power) and a markdown summary.
Usage: python3 -I analysis/bridge_rr/sim_table.py analysis/bridge_rr/sim_rr_1000_2000_4.json"""
import sys, json
r = json.load(open(sys.argv[1])); cells = r["cells"]
order = ["i_T18_viaDelivery", "ix_both_T18_J0.6T", "xiii_boundary_J0.5T", "vi_T12_J0.4T", "ii_T18_beyondDelivery",
         "xiv_boundary_dH0", "x_reversal_T18_dHneg", "iv_T8_harmH", "iii_null"]
short = {"i_T18_viaDelivery": "mainly delivery", "ix_both_T18_J0.6T": "both, $G/T$=0.6", "xiii_boundary_J0.5T": "$G=0.5T$",
         "vi_T12_J0.4T": "$G=0.4T$, $T$=12", "ii_T18_beyondDelivery": "beyond delivery", "xiv_boundary_dH0": "$\\Delta_H=0$",
         "x_reversal_T18_dHneg": "reversal", "iv_T8_harmH": "$T$=8, harm under $H$", "iii_null": "null"}
unk_order = ["u0", "mcar5", "mcar8", "capped_bats8_sil2", "salvaged_bats15"]
unk_short = {"u0": "none", "mcar5": "MCAR 5\\%", "mcar8": "MCAR 8\\%", "capped_bats8_sil2": "MNAR capped 8/2", "salvaged_bats15": "MNAR rescued 15"}
def g(c, k, lab): return c["rates_pct"][k].get(lab, 0.0)
print("% scenario & unknowns & T & dH & G & H1 pass & H2 reduction & H2 reverse & H2b above & H3 pos & H3 neg & fuse")
md = ["| scenario | unknowns | T | ΔH | G | H1 pass | H2 reduction | H2 reverse | H2b above half | H3 pos | H3 neg | fuse |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for s in order:
    for u in unk_order:
        c = cells.get(f"{s}|{u}")
        if not c: continue
        t = c["truth"]; fuse = g(c, "H1", "fuse")
        row = [short[s], unk_short[u], t["T"], t["dH"], t["G"], g(c, "H1", "pass"), g(c, "H2", "reduction"), g(c, "H2", "reverse"),
               g(c, "H2b", "over_half"), g(c, "H3", "positive"), g(c, "H3", "negative"), fuse]
        print(" & ".join(str(x) for x in row) + r" \\")
        md.append("| " + " | ".join(str(x) for x in [s, u] + row[2:]) + " |")
open(sys.argv[1].replace(".json", "_table.md"), "w").write("\n".join(md) + "\n")
print("md written")
