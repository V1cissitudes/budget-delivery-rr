"""Offline tests for the bridge agent (mini-swe-agent 2.1.0 DeterministicToolcallModel + LocalEnvironment + a temp git
repo; no network, no Docker, no API). Run: uv run --no-project --with mini-swe-agent==2.1.0 python bridge/tests/test_bridge_agent.py"""
import json, os, re, subprocess, sys, tempfile, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, ".."))
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
import prompts as P
from agent import BridgeAgent
from outcome import classify
from snapshot import LocalSnapshotter
from spend import Spend

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff"


def repo():
    d = tempfile.mkdtemp()
    g = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
    g("init", "-q"); open(os.path.join(d, "a.py"), "w").write("x = 1\n")
    g("add", "."); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return d


def call(i, cmd, cost=None):
    o = make_toolcall_output(None, [{"id": f"c{i}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}],
                             [{"command": cmd, "tool_call_id": f"c{i}"}])
    if cost is not None:
        o["extra"]["cost"] = cost
    return o


def agent(arm, outputs, **kw):
    d = repo()
    a = BridgeAgent(DeterministicToolcallModel(outputs=outputs), LocalEnvironment(cwd=d), arm=arm, snapshot_fn=LocalSnapshotter(),
                    system_template="sys", instance_template="Fix: {{task}}", step_limit=kw.pop("step_limit", 50),
                    cost_limit=kw.pop("cost_limit", 0), **kw)
    return a, d


def finish(a):
    info = a.run("the bug")
    snap = (a.stop or {}).get("snapshot") or {}
    code, _ = classify(exit_status=info.get("exit_status"), submission=info.get("submission"),
                       limit_kind=(a.stop or {}).get("limit_kind"), workspace_patch=snap.get("patch"), eng_fail=bool(a.eng_fail))
    return info, code


def test_prompt_texts_shared_and_neutral_has_no_digits():
    assert P.intro("bats") == P.intro("number") + "\n" + P.G and P.intro("silent") is None
    assert P.block("number", 7) == P.block("bats", 7) == P.B1.format(u=7, r=43)
    assert not re.search(r"\d", P.N0 + P.B0) and P.block("neutral", 3) == P.block("neutral", 9) == P.B0


def test_number_arm_blocks_and_submission():
    a, d = agent("number", [call(1, "echo hi"), call(2, "printf 'x = 2\\n' > a.py"), call(3, SUBMIT)])
    info, code = finish(a)
    first_user = [m for m in a.messages if m["role"] == "user"][0]["content"]
    assert first_user.endswith(P.I0)
    tools = [m["content"] for m in a.messages if m["role"] == "tool"]
    assert tools[0].endswith(P.B1.format(u=1, r=49)) and tools[1].endswith(P.B1.format(u=2, r=48))
    assert all(r["consistent"] for r in a.budget_log) and len(a.budget_log) == 2
    assert info["exit_status"] == "Submitted" and code == "SUB_NONEMPTY" and a.stop["kind"] == "Submitted"
    assert b"x = 2" in a.stop["snapshot"]["patch"]


def test_silent_arm_untouched():
    a, _ = agent("silent", [call(1, "echo hi"), call(2, SUBMIT)])
    finish(a)
    assert [m for m in a.messages if m["role"] == "user"][0]["content"] == "Fix: the bug"
    assert "<budget>" not in json.dumps(a.messages) and "<status>" not in json.dumps(a.messages)


def test_step_limit_snapshot_has_workspace_diff():
    a, _ = agent("bats", [call(1, "printf 'x = 3\\n' > a.py"), call(2, "echo new > b.txt"), call(3, "echo hi"), call(4, "echo never")],
                 step_limit=3)
    info, code = finish(a)
    assert info["exit_status"] == "LimitsExceeded" and a.n_calls == 3 and a.stop["limit_kind"] == "step"
    p = a.stop["snapshot"]["patch"]
    assert b"x = 3" in p and b"b.txt" in p and code == "CAP_STEP_DIFF"
    assert [m["content"] for m in a.messages if m["role"] == "tool"][-1].endswith(P.B1.format(u=3, r=47))


def test_step_limit_without_diff():
    a, _ = agent("number", [call(1, "echo hi"), call(2, "echo hi")], step_limit=2)
    assert finish(a)[1] == "CAP_STEP_NODIFF"


def test_cost_limit_is_cap_cost():
    a, _ = agent("number", [call(1, "echo a", cost=1.0), call(2, "echo b", cost=1.0), call(3, "echo c")], cost_limit=2.0)
    info, code = finish(a)
    assert info["exit_status"] == "LimitsExceeded" and a.stop["limit_kind"] == "cost" and code == "CAP_COST"


def test_spend_stop_is_engineering_failure():
    sp = Spend(os.path.join(tempfile.mkdtemp(), "spend.json"), 1.5)
    a, _ = agent("number", [call(1, "echo a", cost=1.0), call(2, "echo b", cost=1.0), call(3, "echo c")], spend=sp)
    info = a.run("t")
    assert info["exit_status"] == "SpendStop" and a.n_calls == 2
    assert classify(exit_status="SpendStop", submission="", limit_kind=None, workspace_patch=b"", eng_fail=True)[0] == "ENG_FAIL"


class FlakyModel(DeterministicToolcallModel):
    """First query raises FormatError (as mini's litellm model does on an unparsable response)."""
    def query(self, messages, **kw):
        if not getattr(self, "_flaked", False):
            self._flaked = True
            raise FormatError({"role": "user", "content": "bad format"})
        return super().query(messages, **kw)


def test_format_error_round_still_gets_block():
    d = repo()
    a = BridgeAgent(FlakyModel(outputs=[call(2, SUBMIT)]), LocalEnvironment(cwd=d), arm="number", snapshot_fn=LocalSnapshotter(),
                    system_template="sys", instance_template="Fix: {{task}}", step_limit=50, cost_limit=0)
    finish(a)
    u = [m["content"] for m in a.messages if m["role"] == "user"]
    assert any(c.startswith("bad format") and c.endswith(P.B1.format(u=1, r=49)) for c in u), u


def test_empty_submission_code():
    a, _ = agent("number", [call(1, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    assert finish(a)[1] == "SUB_EMPTY"


if __name__ == "__main__":
    fails = n = 0
    for name, f in sorted(globals().items()):
        if name.startswith("test_"):
            n += 1
            try:
                f(); print("ok  ", name)
            except Exception:
                fails += 1; print("FAIL", name); traceback.print_exc()
    print(f"{n - fails}/{n} passed"); sys.exit(1 if fails else 0)
