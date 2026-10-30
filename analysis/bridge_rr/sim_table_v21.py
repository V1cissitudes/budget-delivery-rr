"""LaTeX rows of Table 'tab:sim' (report v4): H1, H2, H3+, H3-, Fuse under three unknown patterns (none, MCAR 5%,
pilot-matched) at rho = 0.85, plus rho = 0.60 rows for the key scenarios. All rates come from the simulation files.
Usage: python3 -I analysis/bridge_rr/sim_table_v21.py > analysis/bridge_rr/sim_table_v21.tex"""
import os, json, math
HERE = os.path.dirname(os.path.abspath(__file__))
def load(*names):
    cells = {}
    for n in names:
        r = json.load(open(os.path.join(HERE, n))); cells.update({k: dict(v, reps=r["reps"]) for k, v in r["cells"].items()})
    return cells
C85 = load("sim_rr_v2_1000_2000_7.json", "sim_rr_v2_10000_2000_5.json", "sim_rr_v2_4000_2000_6.json",
           "sim_rr_v2_1000_2000_17.json", "sim_rr_v2_10000_2000_15.json", "sim_rr_v2_4000_2000_16.json")
C60 = load("sim_rr_v2_1000_2000_27.json")
def wilson(x, n, z=1.959964):
    p = x / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * (c - h), 100 * (c + h)
def cnt(c, k, lab): return c["counts"][k].get(lab, 0)
def pct(c, k, lab): return 100 * cnt(c, k, lab) / c["reps"]
def fmt(v): return f"{v:.0f}" if v >= 9.95 or v == 0 else f"{v:.1f}"
def err(c, k, lab):
    x = cnt(c, k, lab); lo, hi = wilson(x, c["reps"]); return f"{100*x/c['reps']:.1f} [{lo:.1f}, {hi:.1f}]"
ERR = {"xvii_boundary_G0": [("H2", "reduction"), ("H2", "reverse")], "xiv_boundary_dH0": [("H3", "positive"), ("H3", "negative")],
       "iii_null": [("H2", "reduction"), ("H2", "reverse"), ("H3", "positive"), ("H3", "negative")]}
order = [("i_T18_viaDelivery", "mainly delivery", ["u0", "mcar5", "pilot"]), ("ix_both_T18_J0.6T", "both", ["u0", "mcar5", "pilot"]),
         ("xiii_boundary_J0.5T", "both, $G=0.5T$", ["u0", "pilot"]), ("vi_T12_J0.4T", "both, $T=12$", ["u0", "pilot"]),
         ("ii_T18_beyondDelivery", "beyond delivery", ["u0", "pilot"]), ("xvii_boundary_G0", "boundary $G=0$", ["u0", "mcar5", "pilot"]),
         ("xiv_boundary_dH0", "boundary $\\Delta_H=0$", ["u0", "mcar5", "pilot"]), ("x_reversal_T18_dHneg", "reversal", ["u0", "pilot"]),
         ("iii_null", "null", ["u0", "mcar8", "pilot"])]
US = {"u0": "none", "mcar5": "MCAR 5", "mcar8": "MCAR 8", "pilot": "pilot"}
def row(cells, name, lab, u, head, boundary=True):
    c = cells.get(f"{name}|{u}")
    if not c: return None
    def one(k, l):
        if boundary and u == "u0" and name in ERR and (k, l) in ERR[name]: return err(c, k, l)
        return fmt(pct(c, k, l))
    h2 = one("H2", "reduction"); rv = pct(c, "H2", "reverse")
    if rv >= 0.05 and not (u == "u0" and name in ERR and ("H2", "reverse") in ERR[name]): h2 += f" / {fmt(rv)}"
    return f"{head} & {US[u]} & {fmt(pct(c,'H1','pass'))} & {h2} & {one('H3','positive')} & {one('H3','negative')} & {fmt(pct(c,'H1','fuse'))} \\\\"
rows = []
for name, lab, unks in order:
    first = True
    for u in unks:
        c = C85.get(f"{name}|{u}"); t = c["truth"]
        head = f"{lab} ({fmt(t['T'])}, ${fmt(t['dH'])}$, {fmt(t['G'])})" if first else ""; first = False
        r = row(C85, name, lab, u, head)
        if r: rows.append(r)
rows.append("\\midrule")
for name, lab in [("i_T18_viaDelivery", "mainly delivery"), ("ix_both_T18_J0.6T", "both"), ("xiii_boundary_J0.5T", "both, $G=0.5T$"),
                  ("ii_T18_beyondDelivery", "beyond delivery"), ("xvii_boundary_G0", "boundary $G=0$"), ("xiv_boundary_dH0", "boundary $\\Delta_H=0$")]:
    first = True
    for u in ("u0", "pilot"):
        r = row(C60, name, lab, u, f"$\\rho=0.6$: {lab}" if first else "", boundary=False); first = False
        if r: rows.append(r)
print("\n".join(rows))
