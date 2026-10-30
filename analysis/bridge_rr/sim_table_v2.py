"""LaTeX rows of Table 'tab:sim' in the Stage 1 report, generated from the v2 simulation outputs so that scenario labels,
true parameters and every rate come from the same files. Usage:
python3 -I analysis/bridge_rr/sim_table_v2.py GRID.json NULL.json BOUNDARY.json > analysis/bridge_rr/sim_table_v2.tex"""
import sys, json, math
cells = {}
for f in sys.argv[1:]:
    r = json.load(open(f)); cells.update({k: dict(v, reps=r["reps"]) for k, v in r["cells"].items()})

def wilson(x, n, z=1.959964):
    p = x / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - h), 100 * (c + h)

def cnt(c, k, lab): return c["counts"][k].get(lab, 0)
def pct(c, k, lab): return 100 * cnt(c, k, lab) / c["reps"]
def fmt(v): return f"{v:.0f}" if v >= 9.95 or v == 0 else f"{v:.1f}"
def err(c, k, lab):   # error-direction label with Wilson interval
    x = cnt(c, k, lab); lo, hi = wilson(x, c["reps"]); return f"{100*x/c['reps']:.1f} [{lo:.1f}, {hi:.1f}]"

order = [("i_T18_viaDelivery", "mainly delivery", ["u0", "mcar5", "capped_bats8_sil2"]),
         ("ix_both_T18_J0.6T", "both", ["u0", "mcar5", "capped_bats8_sil2"]),
         ("xiii_boundary_J0.5T", "boundary $G=0.5T$", ["u0", "mcar5", "capped_bats8_sil2"]),
         ("vi_T12_J0.4T", "both, $T=12$", ["u0", "capped_bats8_sil2"]),
         ("ii_T18_beyondDelivery", "beyond delivery", ["u0", "capped_bats8_sil2"]),
         ("xvii_boundary_G0", "boundary $G=0$", ["u0", "mcar5", "capped_bats8_sil2"]),
         ("xiv_boundary_dH0", "boundary $\\Delta_H=0$", ["u0", "mcar5", "capped_bats8_sil2"]),
         ("x_reversal_T18_dHneg", "reversal", ["u0", "capped_bats8_sil2"]),
         ("iii_null", "null", ["u0", "mcar8"])]
unk_short = {"u0": "none", "mcar5": "MCAR 5", "mcar8": "MCAR 8", "capped_bats8_sil2": "MNAR", "salvaged_bats15": "MNAR-r"}
boundary_err = {"xiii_boundary_J0.5T": [("H2b", "over_half"), ("H2b", "under_half")],
                "xvii_boundary_G0": [("H2", "reduction"), ("H2", "reverse")],
                "xiv_boundary_dH0": [("H3", "positive"), ("H3", "negative")],
                "iii_null": [("H2", "reduction"), ("H2", "reverse"), ("H3", "positive"), ("H3", "negative")]}
# H3 columns are printed separately, so the pair logic in cell() applies to H2 / H2b only
rows = []
for name, lab, unks in order:
    first = True
    for u in unks:
        c = cells.get(f"{name}|{u}")
        if not c: continue
        t = c["truth"]
        head = f"{lab} ({fmt(t['T'])}, ${fmt(t['dH'])}$, {fmt(t['G'])})" if first else ""
        first = False
        if name == "xiv_boundary_dH0": head = head.replace("($\\Delta_H=0$", "")
        def cell(k, pos, neg):
            if u == "u0" and name in boundary_err and (k, pos) in boundary_err[name]:
                return err(c, k, pos) + " / " + err(c, k, neg)
            if name == "iii_null" and k == "H2b": return "n.e."
            a, b = pct(c, k, pos), pct(c, k, neg)
            return fmt(a) if b < 0.05 else f"{fmt(a)} / {fmt(b)}"
        def one(k, lab):
            if u == "u0" and name in boundary_err and (k, lab) in boundary_err[name]: return err(c, k, lab)
            return fmt(pct(c, k, lab))
        h1 = fmt(pct(c, "H1", "pass"))
        h2 = cell("H2", "reduction", "reverse")
        h2b = cell("H2b", "over_half", "under_half")
        h3p, h3n = one("H3", "positive"), one("H3", "negative")
        fuse = fmt(pct(c, "H1", "fuse"))
        rows.append(f"{head} & {unk_short[u]} & {h1} & {h2} & {h2b} & {h3p} & {h3n} & {fuse} \\\\")
print("\n".join(rows))
