"""Registered-rule addition (RR Stage 1, v2): a SUBMITTED trajectory whose judge verdict is unknown has H ≡ S by
construction (outcome.py rule 'same_as_S'), so its fill is restricted to (0,0) or (1,1). The sealed fill allowed (0,1)
for any slot with both verdicts unknown, which can create a rescue event that cannot exist. install(tied) patches
analysis_rules.fill in place; every sealed primitive (lower / upper / screen / q1 / signed_claim) calls fill through
the module global, so they all inherit the restriction. Slots not in `tied` are filled exactly as before."""
import analysis_rules as AR

_TIED = set()
_orig_fill = AR.fill


def _fill_tied(data, coef, sense):
    pick = min if sense == "min" else max
    out = {}
    for t, row in data.items():
        new = {"stratum": row["stratum"]}
        for arm, (s, h) in ((a, v) for a, v in row.items() if a != "stratum"):
            cs, ch = coef.get(arm, (0, 0))
            cands = [(0, 0), (1, 1)] if (t, arm) in _TIED and s is None and h is None else AR.feasible(s, h)
            new[arm] = pick(cands, key=lambda p: cs * p[0] + ch * p[1])
        out[t] = new
    return out


def install(tied):
    """tied: iterable of (task_id, arm) whose both-unknown pair must be filled with S = H."""
    _TIED.clear(); _TIED.update(tied); AR.fill = _fill_tied


def uninstall():
    _TIED.clear(); AR.fill = _orig_fill
