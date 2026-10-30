"""Termination taxonomy (prereg DRAFT v3 §4). Maps a finished trajectory to one code and the S/H rule, and turns
judge verdicts into S, H, W with unknown propagation (None = unknown).

注：mini-swe-agent 2.1.0 里没有"连续格式错误就退出"的路径（FormatError 只把错误当作本轮观察，循环继续，
每次仍消耗 1 次调用），所以 FORMAT_EXIT 在 2.1.0 下实际上不会出现；分类照草案保留（见 README 待确认）。"""
SUBMITTED, LIMITS = "Submitted", "LimitsExceeded"
CODES = ("SUB_NONEMPTY", "SUB_EMPTY", "CAP_STEP_DIFF", "CAP_STEP_NODIFF", "CAP_COST", "FORMAT_EXIT", "ENG_FAIL")


def classify(*, exit_status, submission, limit_kind, workspace_patch, eng_fail):
    """limit_kind: 'step' | 'cost' | None (set by the agent when it stops a query at a limit);
    workspace_patch: bytes of F at the end boundary, or None if extraction failed;
    returns (code, rule) where rule tells the scorer what S and H are made of."""
    if eng_fail:
        return "ENG_FAIL", {"S": "unknown", "H": "unknown"}
    if exit_status == SUBMITTED:
        if (submission or "").strip() == "":
            return "SUB_EMPTY", {"S": "zero", "H": "same_as_S"}
        return "SUB_NONEMPTY", {"S": "judge_submission", "H": "same_as_S"}
    if exit_status == LIMITS:
        if limit_kind == "cost":
            return "CAP_COST", {"S": "zero", "H": "judge_F" if workspace_patch is not None else "unknown"}
        if workspace_patch is None:
            return "CAP_STEP_DIFF", {"S": "zero", "H": "unknown"}          # extraction failed => H unknown
        if workspace_patch == b"":
            return "CAP_STEP_NODIFF", {"S": "zero", "H": "zero"}
        return "CAP_STEP_DIFF", {"S": "zero", "H": "judge_F"}
    if exit_status == "FormatExit":
        return "FORMAT_EXIT", {"S": "zero", "H": "judge_F" if workspace_patch is not None else "unknown"}
    return "ENG_FAIL", {"S": "unknown", "H": "unknown"}


def needs(code, rule, f_available):
    """Which products the evaluator must hand to the judge: {'submission': bool, 'F': bool}.
    F is judged for every non-ENG_FAIL trajectory whose F exists, because W (v3) is defined on all trajectories."""
    if code == "ENG_FAIL":
        return {"submission": False, "F": False}
    return {"submission": rule["S"] == "judge_submission", "F": bool(f_available)}


def score(code, rule, *, sub_verdict=None, f_verdict=None, f_available=False):
    """S, H, W for one trajectory (prereg §4 table; v3 ΔW: W = verdict of the end-of-run workspace F for every
    trajectory, submitted or not). Verdicts are 1 / 0 / None (judge unknown => only that verdict is unknown, rule 3).
    ENG_FAIL => everything unknown. F unavailable (extraction/snapshot failure) => H unknown where H uses F (rule 2),
    and W unknown; S is unaffected."""
    if code == "ENG_FAIL":
        return {"S": None, "H": None, "W": None}
    S = {"zero": 0, "judge_submission": sub_verdict, "unknown": None}[rule["S"]]
    H = {"same_as_S": S, "zero": 0, "judge_F": f_verdict, "unknown": None}[rule["H"]]
    W = f_verdict if f_available else None
    if S is not None and H is not None:
        assert S <= H, (code, S, H)
    return {"S": S, "H": H, "W": W}


# ---- blind submission (descriptive only, prereg v4 §4; does not enter any rule) --------------------------------------
# A submitted trajectory is "blind" when no test-like command runs after the last write-like command before the submit
# command. Command-text heuristic, fixed before the seal:
#   write-like: sed -i, redirection to a file (not /dev/null, not &N), tee, patch, git apply/checkout/restore/stash,
#               mv, cp, rm, touch, Python open(..., 'w'/'a'), .write_text(, .write(
#   test-like:  python/python3.x, pytest, tox, nosetests, runtests.py
#   ignored:    commands that mention patch.txt (mini's submission recipe writes the diff there) and the submit command.
# Within one command, a test match after the last write match counts as testing that write.
import re as _re
_WRITE = _re.compile(r"sed\s+-i|>>?\s*(?!/dev/null)(?!&)[\w./~\"'-]|\btee\b|\bpatch\b|git\s+(?:apply|checkout|restore|stash)"
                     r"|\bmv\b|\bcp\b|\brm\b|\btouch\b|open\([^)]*['\"][wa]\+?['\"]|\.write_text\(|\.write\(")
_TEST = _re.compile(r"\b(?:python[0-9.]*|pytest|tox|nosetests|runtests\.py)\b")
SUBMIT_MARK = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def blind_submit(commands):
    """commands: executed command strings in order. None if no submit command or no write-like command before it
    (nothing to have tested); else True (blind) / False."""
    if not any(SUBMIT_MARK in c for c in commands):
        return None
    last_write, tested = None, False
    for c in commands:
        if SUBMIT_MARK in c:
            break
        if "patch.txt" in c:
            continue
        w = [m.end() for m in _WRITE.finditer(c)]
        t = [m.start() for m in _TEST.finditer(c)]
        if w:
            last_write, tested = True, any(x >= w[-1] for x in t)
        elif t and last_write is not None:
            tested = True
    return None if last_write is None else not tested


def commands_from_messages(messages):
    out = []
    for m in messages or []:
        for a in ((m.get("extra") or {}).get("actions") or []):
            if isinstance(a, dict) and isinstance(a.get("command"), str):
                out.append(a["command"])
    return out
