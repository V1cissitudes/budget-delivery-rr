"""端到端离线测试：mini-swe-agent 2.1.0 的真 agent 循环 + 真 LitellmModel 代码路径（litellm.completion 换成离线假响应，
不联网、不调 API）+ LocalEnvironment + 临时 git 仓库 + LocalSnapshotter → run_one.run_trajectory → evaluate（假 judge）
→ batch 驱动。成本按 litellm 1.106 自带的本地价目表计算（LITELLM_LOCAL_MODEL_COST_MAP=True）。
Run: uv run --no-project --with mini-swe-agent==2.1.0 python bridge/tests/test_end_to_end.py"""
import json, os, subprocess, sys
sys.dont_write_bytecode = True
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes as FK  # noqa: E402
import litellm  # noqa: E402
import minisweagent  # noqa: E402
import minisweagent.models.litellm_model as LM  # noqa: E402
from minisweagent.environments.local import LocalEnvironment  # noqa: E402
import identity as ID  # noqa: E402
import prompts as P  # noqa: E402
import run_one as RO  # noqa: E402
import evaluate as EV  # noqa: E402
import batch as BA  # noqa: E402
from model_log import LoggedLitellmModel  # noqa: E402
from snapshot import LocalSnapshotter  # noqa: E402
from spend import Spend  # noqa: E402
import spend as SP  # noqa: E402

CFG = json.load(open(os.path.join(ID.HERE, "config_public_gemini3flash.json")))
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt"
D1_BEFORE = FK.d1_snapshot()


class FakeAPI:
    """Stands in for litellm.completion. script: list of commands (None => a reply without tool calls, which mini
    turns into a FormatError round); versions: j -> model version string returned in the response."""
    def __init__(self, script, versions=lambda j: "gemini-3-flash-preview", raw=None, usage=lambda j: True):
        self.script, self.versions, self.j, self.kwargs = script, versions, 0, []
        self.raw, self.usage = raw or (lambda j: self.versions(j)), usage

    def __call__(self, *, model, messages, tools, **kw):
        self.kwargs.append(dict(kw, model=model))
        cmd = self.script[self.j] if self.j < len(self.script) else "echo idle"
        self.j += 1
        msg = {"role": "assistant", "content": "thinking"}
        if cmd is not None:
            msg["tool_calls"] = [{"id": f"c{self.j}", "type": "function",
                                  "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]
        if kw.get("logger_fn"):                  # litellm calls logger_fn with the raw provider response (post_call)
            kw["logger_fn"]({"original_response": json.dumps({"modelVersion": self.raw(self.j),
                                                              "usageMetadata": {"promptTokenCount": 2000}})})
        usage = ({"prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300,
                  "completion_tokens_details": {"reasoning_tokens": 200},
                  "prompt_tokens_details": {"cached_tokens": 500}} if self.usage(self.j) else None)
        r = litellm.ModelResponse(id=f"resp-{self.j}", model=self.versions(self.j), choices=[
            {"index": 0, "finish_reason": "tool_calls" if cmd else "stop", "message": msg}], usage=usage)
        if usage is None:
            r.usage = None
        return r


def repo():
    d = FK.tmpdir()
    g = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
    g("init", "-q"); open(os.path.join(d, "a.py"), "w").write("x = 1\n")
    g("add", "."); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return d


REGISTRY = ID.load_registry()[0]


def trajectory(arm, script, *, versions=lambda j: "gemini-3-flash-preview", stop_usd=200.0, iid="x__y-1",
               attempt="a1", tdir=None, spend=None, registry=None, raw=None, usage=lambda j: True,
               env=None, snapshotter=None, alive_fn=None, image_ok_after=None, model_hook=None):
    api = FakeAPI(script, versions, raw, usage); LM.litellm.completion = api
    tdir = tdir or FK.tmpdir()
    spend = spend or Spend(os.path.join(FK.tmpdir(), "spend.json"), stop_usd)
    spend.prefix = attempt                         # as run_one does (key_prefix = attempt id) for reconcile()
    model = LoggedLitellmModel(call_log_path=os.path.join(tdir, "calls.json"), registry=registry, **CFG["model"])
    if model_hook:
        model_hook(model)
    rec = {"instance_id": iid, "arm": arm, "attempt": attempt, "k": ID.K, "cost_limit": ID.COST_LIMIT, "dev": False,
           "expected_image_id": FK.IMG, "bridge_code_sha256": ID.bridge_code_sha256(),
           "d1_code_sha256": ID.d1_code_sha256(), "mini_version": minisweagent.__version__}
    rec = RO.run_trajectory(rec=rec, out=tdir, model=model, env=env or LocalEnvironment(cwd=repo()),
                            snapshotter=snapshotter or LocalSnapshotter(), task_text="fix the bug", agent_kwargs=CFG["agent"],
                            spend=spend, alive_fn=alive_fn, image_ok_after=image_ok_after)
    return rec, tdir, model, spend, api


def test_submitted_with_format_error_round_and_version_change():
    rec, tdir, model, spend, api = trajectory(
        "number", ["printf 'FIX = 2\\n' > a.py", None, "git diff -- a.py > patch.txt", SUBMIT],
        versions=lambda j: "gemini-3-flash-preview" if j <= 2 else "gemini/gemini-3-flash-preview")
    assert minisweagent.__version__ == "2.1.0" and rec["code"] == "SUB_NONEMPTY" and rec["n_calls"] == 4
    calls = json.load(open(os.path.join(tdir, "calls.json")))
    assert [c["model_version"] for c in calls] == ["gemini-3-flash-preview"] * 2 + ["gemini/gemini-3-flash-preview"] * 2
    assert all(c["cost"] and c["cost"] > 0 and c["t_response"] >= c["t_request"] for c in calls)
    assert rec["model_versions"] == ["gemini-3-flash-preview", "gemini/gemini-3-flash-preview"]
    assert rec["tokens"] == {"prompt": 8000, "completion": 1200, "reasoning": 800, "cached": 2000}
    # spend counts the FormatError round too; mini's own agent.cost does not
    usd = json.load(open(spend.path))["usd"]
    assert abs(usd - sum(c["cost"] for c in calls)) < 1e-12 and usd > rec["agent_cost_usd"]
    assert abs(rec["agent_cost_usd"] - sum(c["cost"] for c in calls if c["i"] != 2)) < 1e-12
    # the frozen public model kwargs reached the API call unchanged
    assert api.kwargs[0]["reasoning_effort"] == "high" and api.kwargs[0]["model"] == "gemini/gemini-3-flash-preview"
    traj = json.load(open(os.path.join(tdir, "traj.json")))
    users = [m["content"] for m in traj["messages"] if m["role"] == "user"]
    assert users[0].endswith(P.I0) and any(u.startswith("Tool call error") and u.endswith(P.B1.format(u=2, r=48)) for u in users)
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="number", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert (r["code"], r["S"], r["H"], r["W"]) == ("SUB_NONEMPTY", 1, 1, 1)
    assert r["identity"]["model_versions"] == rec["model_versions"] and r["identity"]["mini_version"] == "2.1.0"
    assert b"patch.txt" in open(os.path.join(tdir, "F_end.patch"), "rb").read()       # F keeps every file (F-v6 rule)


def test_unpriced_served_version_is_eng_fail():
    # (legacy path, no registry; a string no other test registers: litellm.register_model is process-global)
    # litellm 1.106 prices by response.model (the served version). A version string missing from its price map gives
    # cost 0, and mini's cost_tracking="default" (frozen public config) then raises => ENG_FAIL. Dry-run item.
    rec, tdir, model, *_ = trajectory("number", ["echo a", SUBMIT], versions=lambda j: "gemini-3-flash-preview-2027-01")
    assert rec["code"] == "ENG_FAIL" and "EXCEPTION" in rec["why"] and "Cost must be > 0.0" in rec["error"]
    assert model.calls[0]["model_version"] == "gemini-3-flash-preview-2027-01" and model.calls[0]["cost"] is None


def test_step_cap_takes_F_while_alive():
    rec, tdir, *_ = trajectory("bats", ["printf 'FIX = 3\\n' > a.py"] + ["echo hi"] * 60)
    assert rec["code"] == "CAP_STEP_DIFF" and rec["n_calls"] == 50 and rec["limit_kind"] == "step"
    assert rec["budget_log"][-1]["shown"] == P.B1.format(u=50, r=0) and all(b["consistent"] for b in rec["budget_log"])
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="bats", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert (r["S"], r["H"], r["W"]) == (0, 1, 1)


def test_spend_stop_mid_trajectory_is_resource_stop():
    rec, tdir, model, spend, _ = trajectory("silent", ["echo a", "echo b", "echo c"], stop_usd=0.002)
    assert rec["exit_status"] == "SpendStop" and rec["resource_stop"] and rec["code"] == "ENG_FAIL"
    assert len(model.calls) == 2                                     # each fake call costs about $0.0018
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert r["resource_stop"] and r["S"] is None and not BA.rerun_eligible(r)


def test_batch_end_to_end_four_arms():
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 200.0); judge = FK.FakeJudge()
    plans = {"silent": ["printf 'FIX\\n' > a.py", "echo hi"], "neutral": ["git status", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"],
             "number": ["printf 'FIX\\n' > a.py", "git diff > patch.txt", SUBMIT], "bats": [None, "echo hi"]}

    def run_fn(*, iid, arm, attempt, tdir, image_id):
        trajectory(arm, plans[arm] + ["echo hi"] * 60, iid=iid, attempt=attempt, tdir=tdir, spend=sp)
        return 0
    b = BA.Batch(out=out, ids=["x__y-1"], arms=list(P.ARMS), pinned={"x__y-1": {"pinned_id": FK.IMG}}, spend=sp,
                 run_fn=run_fn, eval_fn=lambda tdir, **kw: EV.evaluate(tdir, judge_fn=judge, **kw),
                 image_id_fn=lambda iid: FK.IMG)
    assert b.run() == "END"
    res = {a: json.load(open(os.path.join(out, "x__y-1", a, "result.json"))) for a in P.ARMS}
    assert {a: (r["code"], r["S"], r["H"], r["W"]) for a, r in res.items()} == {
        "silent": ("CAP_STEP_DIFF", 0, 1, 1), "neutral": ("SUB_EMPTY", 0, 0, 0),
        "number": ("SUB_NONEMPTY", 1, 1, 1), "bats": ("CAP_STEP_NODIFF", 0, 0, 0)}
    led = [json.loads(l) for l in open(os.path.join(out, "ledger.jsonl"))]
    ends = [e for e in led if e["event"] == "run_end"]
    assert [e["arm"] for e in ends] == BA.arm_order("x__y-1", list(P.ARMS))
    assert all(e["model_versions"] == ["gemini-3-flash-preview"] for e in ends)
    assert abs(json.load(open(sp.path))["usd"] - sum(r["run"]["api_cost_usd"] for r in res.values())) < 1e-9
    b2 = BA.Batch(out=out, ids=["x__y-1"], arms=list(P.ARMS), pinned={"x__y-1": {"pinned_id": FK.IMG}}, spend=sp,
                  run_fn=lambda **k: 1 / 0, eval_fn=None, image_id_fn=lambda iid: FK.IMG)
    assert b2.run() == "END"                                                        # resume: nothing reruns


# ---- v4 §7.3 pricing guard (registry = bridge/pricing_registry.json) -------------------------------------------------
def test_registered_version_priced_and_raw_version_kept():
    rec, tdir, model, spend, api = trajectory("number", ["echo a", "printf 'FIX\\n' > a.py", "git diff > patch.txt", SUBMIT],
                                              registry=REGISTRY, raw=lambda j: "gemini-3-flash-preview@2026-09-30")
    assert rec["code"] == "SUB_NONEMPTY" and not rec["pricing_failure"] and rec["unpriced_calls"] == 0
    c = model.calls[0]
    assert c["pricing_status"] == "OK" and abs(c["cost"] - 0.001675) < 1e-12 and abs(c["cost_registry"] - c["cost"]) < 1e-12
    assert rec["raw_model_versions"] == ["gemini-3-flash-preview@2026-09-30"] and rec["model_versions"] == ["gemini-3-flash-preview"]
    st = json.load(open(spend.path))
    assert abs(st["usd"] - 4 * 0.001675) < 1e-12 and st["usd_provisional"] == 0 and st["blocked"] is None
    assert rec["blind_submit"] is True                     # wrote a.py, never ran python/pytest before submitting


def test_unregistered_version_is_not_zero_cost():
    # second response comes from a version string missing from the registry (and from litellm's price map)
    rec, tdir, model, spend, _ = trajectory("bats", ["echo a", "echo b", "echo c"], registry=REGISTRY,
                                            versions=lambda j: "gemini-3-flash-preview" if j == 1 else "gemini-3-flash-preview-2026-10")
    assert rec["code"] == "ENG_FAIL" and "PRICING_UNAVAILABLE" in rec["why"] and rec["pricing_failure"] == "UNREGISTERED_VERSION"
    assert [c["pricing_status"] for c in model.calls] == ["OK", "UNREGISTERED_VERSION"] and model.calls[1]["cost"] is None
    st = json.load(open(spend.path))
    assert abs(st["usd"] - 0.001675) < 1e-12 and st["usd_provisional"] == 1.0 and "UNREGISTERED_VERSION" in st["blocked"]["reason"]
    assert rec["unpriced_calls"] == 1 and rec["provisional_usd"] == 1.0
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="bats", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert r["pricing_failure"] == "UNREGISTERED_VERSION" and r["S"] is None and r["run"]["provisional_usd"] == 1.0


def test_missing_usage_is_not_zero_cost():
    rec, tdir, model, spend, _ = trajectory("silent", ["echo a", "echo b"], registry=REGISTRY, usage=lambda j: j != 1)
    assert rec["pricing_failure"] == "NO_USAGE" and model.calls[0]["cost"] is None
    assert json.load(open(spend.path))["usd_provisional"] == 1.0 and Spend(spend.path, 200.0).blocked()


def test_suffixed_version_silently_priced_by_litellm_is_caught():
    # litellm 1.106 prices 'gemini-3-flash-preview-0930' (and -001, -20261001) at the base price without any error,
    # i.e. a silent mapping to an unconfirmed price; the registry check stops it
    rec, _, model, spend, _ = trajectory("number", ["echo a", "echo b"], registry=REGISTRY,
                                         versions=lambda j: "gemini-3-flash-preview" if j == 1 else "gemini-3-flash-preview-0930")
    assert model.calls[1]["cost_litellm"] > 0 and model.calls[1]["pricing_status"] == "UNREGISTERED_VERSION"
    assert rec["pricing_failure"] == "UNREGISTERED_VERSION" and json.load(open(spend.path))["blocked"]


def test_registered_alternate_version_continues_and_is_logged():
    # a version string litellm itself cannot price (it returns 0 for '-2026-10'); registering it in the registry alone
    # (no config change, no code-identity change) is enough: the model registers the prices with litellm
    reg = json.loads(json.dumps(REGISTRY)); reg["versions"]["gemini-3-flash-preview-2026-10"] = dict(reg["versions"]["gemini-3-flash-preview"])
    code0 = ID.bridge_code_sha256()
    rec, _, model, *_ = trajectory("number", ["printf 'FIX\\n' > a.py", "git diff > patch.txt", SUBMIT], registry=reg,
                                   versions=lambda j: "gemini-3-flash-preview" if j == 1 else "gemini-3-flash-preview-2026-10")
    assert not rec["pricing_failure"] and rec["code"] == "SUB_NONEMPTY" and ID.bridge_code_sha256() == code0
    assert rec["model_versions"] == ["gemini-3-flash-preview", "gemini-3-flash-preview-2026-10"]
    assert all(c["pricing_status"] == "OK" and abs(c["cost"] - 0.001675) < 1e-12 for c in model.calls)


def test_batch_pauses_on_unpriced_call_and_reruns_once_after_clear():
    out = FK.tmpdir(); sp = Spend(os.path.join(out, "spend.json"), 200.0, dev=False); judge = FK.FakeJudge()
    state = {"bad": True, "runs": []}

    def run_fn(*, iid, arm, attempt, tdir, image_id):
        state["runs"].append((iid, arm))
        v = (lambda j: "gemini-3-unknown") if state["bad"] else (lambda j: "gemini-3-flash-preview")
        trajectory(arm, ["echo a", SUBMIT], iid=iid, attempt=attempt, tdir=tdir, spend=sp, registry=REGISTRY, versions=v)
        return 0
    mk = lambda: BA.Batch(out=out, ids=["x__y-1", "x__y-2"], arms=["silent", "bats"],
                          pinned={i: {"pinned_id": FK.IMG} for i in ("x__y-1", "x__y-2")}, spend=sp, run_fn=run_fn,
                          eval_fn=lambda tdir, **kw: EV.evaluate(tdir, judge_fn=judge, **kw), image_id_fn=lambda iid: FK.IMG)
    assert mk().run() == "STOPPED" and len(state["runs"]) == 1                       # paused: no rerun, no next slot
    led = [json.loads(l) for l in open(os.path.join(out, "ledger.jsonl"))]
    assert led[-2]["event"] == "STOP_PRICING"
    assert mk().run() == "STOPPED" and len(state["runs"]) == 1                       # still paused until cleared
    probs = SP.reconcile(out, sp.path)
    assert any(p.startswith("OPEN_PROVISIONAL") for p in probs), probs                # the pause is also visible offline
    sp.clear("author checked bill; version string registered", reconciled_usd=0.002, bill_id="ais-2026-10-20-a")
    assert SP.reconcile(out, sp.path) == []
    state["bad"] = False
    assert mk().run() == "END" and len(state["runs"]) == 5                           # 1 rerun + 3 remaining slots
    first = state["runs"][0]
    r = json.load(open(os.path.join(out, first[0], first[1], "result.json")))
    assert r["code"] == "CAP_STEP_NODIFF" and len(r["attempt_history"]) == 1         # the paused slot used its one rerun
    st = json.load(open(sp.path))
    assert st["usd_provisional"] == 0 and st["history"][0]["reconciled_usd"] == 0.002



# ---------------- addendum 2: failure-path review (2026-10-20) ----------------
class DeadAfterEnv(LocalEnvironment):
    """review B1: the container vanishes after n_ok commands; docker's own error comes back as ordinary output."""
    def __init__(self, n_ok=3, **kw):
        super().__init__(**kw); self.n_ok, self.k = n_ok, 0

    def execute(self, action, cwd="", *, timeout=None):
        self.k += 1
        if self.k <= self.n_ok:
            return super().execute(action, cwd, timeout=timeout)
        return {"output": "Error response from daemon: No such container: c0\n", "returncode": 1, "exception_info": ""}


class DeadSnap:
    def __call__(self, env):
        raise subprocess.CalledProcessError(1, ["docker", "cp"])


def test_container_gone_is_eng_fail_and_aborts_early():
    # review B1: never CAP_STEP_DIFF with a definite S = 0; the early abort also saves the remaining calls
    rec, tdir, *_ = trajectory("silent", ["echo hi"] * 60, env=DeadAfterEnv(cwd=repo()), snapshotter=DeadSnap(),
                               alive_fn=lambda e: False)
    assert rec["code"] == "ENG_FAIL" and "CONTAINER_NOT_RUNNING" in rec["why"], (rec["code"], rec["why"])
    assert rec["n_calls"] == 4 and rec["container_running_at_stop"] is False
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert (r["code"], r["S"], r["H"], r["W"]) == ("ENG_FAIL", None, None, None) and BA.rerun_eligible(r)


def test_container_state_unknown_at_stop_is_eng_fail():
    # the liveness check itself fails (None): no early abort, but the stop is an engineering failure (conservative)
    rec, *_ = trajectory("silent", ["echo hi"] * 60, env=DeadAfterEnv(cwd=repo()), snapshotter=DeadSnap(),
                         alive_fn=lambda e: None)
    assert rec["code"] == "ENG_FAIL" and "CONTAINER_STATE_UNKNOWN" in rec["why"] and rec["n_calls"] == 50


def test_container_alive_snapshot_failure_keeps_rule_2():
    # container still running, only the snapshot failed: unchanged §4 rule 2 (S = 0, H/W unknown, not ENG_FAIL)
    rec, *_ = trajectory("silent", ["echo hi"] * 60, snapshotter=DeadSnap(), alive_fn=lambda e: True)
    assert rec["code"] == "CAP_STEP_DIFF" and rec["rule"] == {"S": "zero", "H": "unknown"} and rec["why"] == []


def test_docker_alive_parses_real_docker_messages():
    # found on the desktop with real Docker 29 (tools/check_b1_docker.py, 2026-10-20): a vanished container gives
    # "error: no such object: <id>" in lowercase, which the case-sensitive check took for a failed check (None)
    orig = subprocess.run
    def fake(rc, out="", err=""):
        return lambda *a, **k: subprocess.CompletedProcess(a[0], rc, out, err)
    try:
        for rc, out, err, want in [(0, "true\n", "", True), (0, "false\n", "", False),
                                   (1, "\n", "error: no such object: " + "0" * 64 + "\n", False),     # Docker 29
                                   (1, "", "Error: No such object: abc\n", False),                     # older clients
                                   (1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n", None),
                                   (0, "weird\n", "", None)]:
            subprocess.run = fake(rc, out, err)
            assert RO.docker_alive("abc") is want, (rc, out, err)
    finally:
        subprocess.run = orig


def test_container_gone_lowercase_daemon_message_aborts_early():
    # the early abort matches docker's messages case-insensitively (real output: "Error response from daemon: No such
    # container: <id>" / "... container <id> is not running")
    class LowerDeadEnv(DeadAfterEnv):
        def execute(self, action, **kw):
            r = super().execute(action, **kw)
            if r.get("returncode"):
                r["output"] = r["output"].lower()
            return r
    rec, *_ = trajectory("silent", ["echo hi"] * 60, env=LowerDeadEnv(cwd=repo()), snapshotter=DeadSnap(),
                         alive_fn=lambda e: False)
    assert rec["code"] == "ENG_FAIL" and "CONTAINER_NOT_RUNNING" in rec["why"] and rec["n_calls"] == 4


def test_fb1_final_save_failure_keeps_record():
    # panoramic ②: F_end.patch is written, then writing F_b1.patch fails (a directory sits where the file should go);
    # record.json must still be written with F-v6 intact and F-b1 marked unavailable (no ENG_FAIL, no agent rerun)
    tdir = FK.tmpdir(); os.makedirs(os.path.join(tdir, "F_b1.patch"))
    rec, tdir, *_ = trajectory("silent", ["printf 'FIX\\n' > a.py", "git diff > patch.txt", SUBMIT], tdir=tdir)
    assert rec["code"] == "SUB_NONEMPTY" and rec["F"]["available"] and os.path.exists(os.path.join(tdir, "F_end.patch"))
    assert rec["F_b1"]["available"] is False and "error" in rec["F_b1"] and rec["why"] == []
    assert json.load(open(os.path.join(tdir, "record.json")))["code"] == "SUB_NONEMPTY"
    r = EV.evaluate(tdir, instance_id="x__y-1", arm="silent", attempt="a1", image_id=FK.IMG, judge_fn=FK.FakeJudge())
    assert (r["code"], r["S"], r["H"]) == ("SUB_NONEMPTY", 1, 1)


def test_image_status_separates_drift_from_check_failure():
    # review m2
    seq = iter([None, FK.IMG])
    assert RO.image_status(lambda: next(seq), FK.IMG, sleep=lambda s: None) == "OK"
    assert RO.image_status(lambda: None, FK.IMG, sleep=lambda s: None) == "CHECK_FAILED"
    assert RO.image_status(lambda: "sha256:" + "cd" * 32, FK.IMG, sleep=lambda s: None) == "DRIFT"
    def boom():
        raise subprocess.TimeoutExpired(["docker"], 60)
    assert RO.image_status(boom, FK.IMG, sleep=lambda s: None) == "CHECK_FAILED"
    rec, *_ = trajectory("silent", ["printf 'FIX\\n' > a.py", "git diff > patch.txt", SUBMIT], image_ok_after=lambda: "CHECK_FAILED")
    assert rec["code"] == "ENG_FAIL" and "IMAGE_CHECK_FAILED" in rec["why"] and "IMAGE_DRIFT_DURING_RUN" not in rec["why"]


def test_call_log_write_failure_never_resends():
    # review m3: a failed calls.json write after a billed response aborts instead of being retried (re-billed)
    def hook(m):
        orig, st = m._write, {"n": 0}
        def w():
            st["n"] += 1
            if st["n"] == 1:
                raise OSError("disk full")
            return orig()
        m._write = w
    rec, tdir, model, spend, api = trajectory("silent", ["echo a", "echo b"], model_hook=hook)
    assert api.j == 1 and rec["code"] == "ENG_FAIL"
    assert spend.blocked() and json.load(open(spend.path))["usd_provisional"] == 1.0   # the billed call is still charged


class FailingSpend(Spend):
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.n = 0

    def add(self, usd, provisional=False, key=None):
        self.n += 1
        if self.n == 1:
            raise OSError("ledger write failed")
        return super().add(usd, provisional=provisional, key=key)


def test_accounting_error_pauses_ledger():
    # review m12 (C2-3 second part): an accounting error blocks the ledger before re-raising
    sp = FailingSpend(os.path.join(FK.tmpdir(), "spend.json"), 200.0)
    rec, *_ = trajectory("silent", ["echo a", "echo b"], spend=sp)
    assert rec["code"] == "ENG_FAIL" and sp.blocked() and "accounting error" in sp.blocked()["reason"]


def test_env_check_against_lock():
    # review I3
    from importlib.metadata import PackageNotFoundError
    assert RO.check_env(pins={"litellm": "1.106.0"}, version_fn=lambda n: "1.106.0") == []
    assert RO.check_env(pins={"litellm": "1.106.0"}, version_fn=lambda n: "1.107.0") == ["litellm: lock 1.106.0, installed 1.107.0"]
    def missing(n):
        raise PackageNotFoundError(n)
    assert RO.check_env(pins={"mcp": "2.3.0"}, version_fn=missing) == ["mcp: lock 2.3.0, installed None"]
    pins = ID.env_lock_pins()
    assert pins["litellm"] == "1.106.0" and pins["mini-swe-agent"] == "2.1.0" and pins["mcp"] == "2.3.0"


def test_d1_untouched():
    assert ID.d1_code_sha256() == ID.D1_EXPECTED and FK.d1_snapshot() == D1_BEFORE


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
