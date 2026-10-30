"""Counts and 95% Wilson intervals for the null and boundary cells. Usage: python3 -I wilson.py null.json boundary.json"""
import sys, json, math
def wilson(k, n, z=1.959964):
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100 * (c - h) / d, 100 * (c + h) / d
for f in sys.argv[1:]:
    r = json.load(open(f)); n = r["reps"]
    for cell, c in r["cells"].items():
        out = []
        for k, lab in (("H1", "pass"), ("H2", "reduction"), ("H2", "reverse"), ("H2b", "over_half"), ("H2b", "under_half"), ("H3", "positive"), ("H3", "negative"), ("H1", "fuse")):
            pct = c["rates_pct"][k].get(lab, 0.0); cnt = round(pct * n / 100); lo, hi = wilson(cnt, n)
            out.append(f"{k}:{lab}={cnt}/{n} ({pct:.2f}% [{lo:.2f},{hi:.2f}])")
        print(cell, "truth", c["truth"]); print("   ", "; ".join(out))
