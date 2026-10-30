"""BridgeAgent: mini-swe-agent 2.1.0 DefaultAgent plus (1) the arm's text appended to the first user message and to
the last observation of each round, (2) a per-round log of the displayed budget vs the agent's own counter,
(3) a workspace snapshot taken INSIDE step() when the run is about to stop (submission, limit, error), while the
container is still alive (mini handles InterruptAgentFlow inside run(), so an outer hook would be too late),
(4) a spend ledger with a stop line and a pricing pause (v4 §7.3). mini's own source is not modified. DRAFT: not sealed."""
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded
import prompts as P

PROVISIONAL_DEFAULT = 1.0     # used only if a call record lacks its own provisional_usd (should not happen)


DAEMON_GONE = ("no such container", "is not running", "cannot connect to the docker daemon")   # matched lowercased


class ContainerGone(RuntimeError):
    """The container disappeared mid-trajectory (review B1): an engineering failure, never an agent outcome."""


class SpendStop(InterruptAgentFlow):
    """Run-wide spend stop line reached before a query (resource stop; trajectory outcome unknown)."""


class BridgeAgent(DefaultAgent):
    def __init__(self, model, env, *, arm, snapshot_fn, spend=None, alive_fn=None, **kwargs):
        assert arm in P.ARMS
        intro = P.intro(arm)
        if intro:
            kwargs["instance_template"] = kwargs["instance_template"] + "\n\n" + intro
        super().__init__(model, env, **kwargs)
        self.arm, self.snapshot_fn, self.spend = arm, snapshot_fn, spend
        # alive_fn(env) -> True / False / None (check failed); None for offline local environments (no check)
        self.alive_fn = alive_fn
        self.budget_log, self.stop = [], None      # stop = {"kind", "n_calls", "snapshot", "container_running"}
        self.eng_fail = None
        self.container_problem = None              # "CONTAINER_NOT_RUNNING" / "CONTAINER_STATE_UNKNOWN" (review B1)

    # --- limits: identical rule to mini (checked before the query), but remember which limit fired
    def query(self):
        if self.spend is not None and self.spend.exhausted():
            raise SpendStop({"role": "exit", "content": "SpendStop", "extra": {"exit_status": "SpendStop", "submission": ""}})
        step_hit = 0 < self.config.step_limit <= self.n_calls
        cost_hit = 0 < self.config.cost_limit <= self.cost
        if step_hit or cost_hit:
            self.limit_kind = "step" if step_hit else "cost"
        before, n0 = self.cost, len(getattr(self.model, "calls", None) or [])
        try:
            return super().query()                # raises LimitsExceeded exactly as mini does
        finally:
            # Spend comes from the API usage of every response, including a FormatError round whose cost mini never
            # adds to agent.cost (LoggedLitellmModel.calls); models without a call log fall back to agent.cost.
            # v4 §7.3: a billed response without a confirmed price is never counted as zero: its provisional
            # reservation goes into the ledger and the ledger is paused, so the batch stops before the next slot.
            if self.spend is not None:
                try:
                    calls = getattr(self.model, "calls", None)
                    if calls is None:
                        if self.cost - before:
                            self.spend.add(self.cost - before)
                    else:
                        for c in calls[n0:]:
                            key = self.spend.call_key(c) if hasattr(self.spend, "call_key") else None
                            if c.get("cost") is not None:
                                self.spend.add(c["cost"], key=key)
                            else:
                                self.spend.add(c.get("provisional_usd") or PROVISIONAL_DEFAULT, provisional=True, key=key)
                        unpriced = [c for c in calls[n0:] if c.get("cost") is None]
                        if unpriced:
                            self.spend.block(f"unpriced response(s): {[c.get('pricing_status') for c in unpriced]} "
                                             f"served={[c.get('model_version') for c in unpriced]}")
                except Exception as e:             # review C2-3: an accounting error pauses the ledger before re-raising,
                    try:                           # so the batch cannot launch another attempt on a broken ledger
                        self.spend.block(f"accounting error: {type(e).__name__}: {e}")
                    except Exception:
                        pass
                    raise

    def _append(self, msgs):
        b = P.block(self.arm, self.n_calls)
        shown = None
        if b and msgs and isinstance(msgs[-1].get("content"), str):
            msgs[-1]["content"] = msgs[-1]["content"] + "\n" + b
            shown = b
        self.budget_log.append({"n_calls": self.n_calls, "shown": shown,
                                "consistent": (shown is None) == (b is None) and
                                              (self.arm in ("silent", "neutral") or f"Calls Used: {self.n_calls}," in (shown or ""))})
        return msgs

    def execute_actions(self, message):
        outputs = [self.env.execute(a) for a in message.get("extra", {}).get("actions", [])]
        # review B1 (optional early abort): mini returns docker's own errors as ordinary output; if a command reports a
        # vanished container and the liveness check confirms it, stop now instead of spending the remaining calls
        if self.alive_fn is not None and any(o.get("returncode") not in (0, None) and any(m in (o.get("output") or "").lower() for m in DAEMON_GONE)
                                             for o in outputs):
            if self.alive_fn(self.env) is False:
                raise ContainerGone("container is not running (docker reported it gone during the trajectory)")
        msgs = self.model.format_observation_messages(message, outputs, self.get_template_vars())
        return self.add_messages(*self._append(list(msgs)))

    def step(self):
        try:
            return super().step()
        except FormatError as e:                   # mini adds these as the round's "observation": keep arms informed
            e.messages = tuple(self._append([dict(m) for m in e.messages]))
            raise
        except InterruptAgentFlow as e:            # Submitted / LimitsExceeded / SpendStop: container still alive
            kind = e.messages[-1].get("extra", {}).get("exit_status") if e.messages else None
            self._snap(kind)
            raise
        except Exception as e:                     # uncaught error => engineering failure; try a snapshot anyway
            self.eng_fail = f"{type(e).__name__}: {e}"
            self._snap("error")
            raise

    def _snap(self, kind):
        if self.stop is not None:
            return
        # review B1: check the container is still running at the cut-off boundary, BEFORE and independently of the
        # snapshot (a snapshot exception must not hide it). Not running => the trajectory is an engineering failure
        # (prereg §4 table: "容器失败" => ENG_FAIL); a failed check is also an engineering failure (conservative).
        running = None
        if self.alive_fn is not None:
            try:
                running = self.alive_fn(self.env)
            except Exception:
                running = None
            if running is False:
                self.container_problem = "CONTAINER_NOT_RUNNING"
            elif running is None:
                self.container_problem = "CONTAINER_STATE_UNKNOWN"
        try:
            snap = self.snapshot_fn(self.env)
        except Exception as e:                     # F extraction failure => H unknown (prereg §4 rule 2)
            snap = {"error": f"{type(e).__name__}: {e}"}
        self.stop = {"kind": kind, "limit_kind": getattr(self, "limit_kind", None), "n_calls": self.n_calls, "snapshot": snap,
                     "container_running": running if self.alive_fn is not None else "not_checked"}
