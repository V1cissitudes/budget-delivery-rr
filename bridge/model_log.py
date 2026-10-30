"""逐次调用记录与计价保护（草案 v4 §3、§7.3）。草稿，未封存。

LoggedLitellmModel 继承 mini 2.1.0 的 LitellmModel，不改 mini 源码，只在两处挂钩：
- `_query`：每拿到一次成功响应（mini 的重试在它外层，失败的尝试不会到这里），记一条：序号、请求与响应时间、
  `response.model`（litellm 1.106 对 Gemini 把 `modelVersion` 中 "@" 之后的部分去掉后写进这里）、原始响应里的
  `modelVersion` 与 `usageMetadata`（经 litellm 的 logger_fn 取原文，只留这两项）、响应 id、created、usage 原样。
- `_calculate_cost`：计价保护。litellm 1.106 按 `response.model` 定价，版本串不在价目表里或缺 usage 时静默返回 0
  （已离线核实）。所以这里先按 bridge/pricing_registry.json 判定：
    OK                      response.model 已登记、usage 齐全，litellm 的成本与登记价算出的成本在容差内一致
    NO_USAGE                usage 缺失或 prompt/completion token 都为 0
    UNREGISTERED_VERSION    response.model 不在登记表（包括版本漂移到未经确认的串）
    LITELLM_COST_FAILED     litellm 算不出成本（异常或 ≤ 0）
    PRICE_MISMATCH          litellm 成本与登记价算出的成本不一致
  非 OK：这次调用的成本记为未知（cost=None），另记临时预留 provisional_usd（登记表给定，不是价格），然后抛
  PricingUnavailable。BridgeAgent 把预留记入支出账本并把账本置为“计价暂停”，整批在下一槽之前停下（batch.py），
  不立即重跑、不继续花钱；作者核对账单、确认价格后才能清除暂停（spend.py）。未知成本从不当作 0，也不映射到
  未经确认的价格。
mini 先算成本、后解析工具调用，所以格式错误的那一轮也会被记下成本。每次更新都原子地重写 calls.json。"""
import json, os, time
from minisweagent.models.litellm_model import LitellmModel


class CallLogWriteError(RuntimeError):
    """calls.json could not be written after a (billed) response. Listed in abort_exceptions so mini's retry loop does
    NOT re-send the request (review m3); the response stays in memory and is still charged by BridgeAgent."""


class PricingUnavailable(RuntimeError):
    """The cost of a billed response could not be determined from confirmed prices (prereg v4 §7.3)."""


def _dump(x):
    if x is None:
        return None
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if isinstance(x, dict):
        return x
    return dict(x) if hasattr(x, "keys") else str(x)


def tokens(usage):
    """(prompt, completion, cached) from an OpenAI-style usage dict; None entries stay None."""
    u = usage or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("cache_read_input_tokens") or 0
    return u.get("prompt_tokens"), u.get("completion_tokens"), cached


def registry_cost(entry, usage):
    p, c, cached = tokens(usage)
    return ((p - cached) * entry["input_per_token"] + cached * entry["cache_read_per_token"]
            + c * entry["output_per_token"])


def price_status(registry, served, usage, litellm_cost):
    """Returns (status, registry_cost_or_None). Pure function (unit-tested)."""
    p, c, _ = tokens(usage)
    if not usage or p is None or c is None or (not p and not c):
        return "NO_USAGE", None
    entry = (registry.get("versions") or {}).get(served)
    if entry is None:
        return "UNREGISTERED_VERSION", None
    rc = registry_cost(entry, usage)
    if litellm_cost is None or litellm_cost <= 0:
        return "LITELLM_COST_FAILED", rc
    if abs(litellm_cost - rc) > registry.get("relative_tolerance", 0.01) * rc:
        return "PRICE_MISMATCH", rc
    return "OK", rc


def register_with_litellm(registry):
    """Give litellm the registered prices for every registered served string (bare and 'gemini/'-prefixed), so a newly
    registered version string that litellm does not know can be priced, and mini's $3 cap keeps working, by editing
    pricing_registry.json alone (no change to the frozen public config or to the bridge code identity)."""
    import litellm
    entries = {}
    for v, e in (registry.get("versions") or {}).items():
        spec = {"input_cost_per_token": e["input_per_token"], "output_cost_per_token": e["output_per_token"],
                "output_cost_per_reasoning_token": e["output_per_token"],
                "cache_read_input_token_cost": e["cache_read_per_token"], "litellm_provider": "gemini", "mode": "chat"}
        for k in (v, "gemini/" + v):
            entries[k] = spec
    litellm.register_model(entries)
    return sorted(entries)


class LoggedLitellmModel(LitellmModel):
    abort_exceptions = list(LitellmModel.abort_exceptions) + [CallLogWriteError]

    def __init__(self, *, call_log_path=None, registry=None, **kwargs):
        super().__init__(**kwargs)
        self.calls, self.call_log_path = [], call_log_path
        self.registry = registry
        if registry:
            register_with_litellm(registry)
        self.pricing_failure = None
        self._raw = None

    def _write(self):
        if self.call_log_path:
            tmp = self.call_log_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.calls, f, indent=1, default=str); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.call_log_path)

    def _capture(self, details):
        """litellm logger_fn: called synchronously pre- and post-call; keep only modelVersion and usageMetadata."""
        raw = (details or {}).get("original_response")
        if not raw:
            return
        try:
            j = json.loads(raw) if isinstance(raw, str) else raw
            self._raw = {"modelVersion": j.get("modelVersion"), "usageMetadata": j.get("usageMetadata")}
        except Exception:
            self._raw = {"unparsed": True}

    def _query(self, messages, **kwargs):
        t0 = time.time(); self._raw = None
        resp = super()._query(messages, logger_fn=self._capture, **kwargs)
        raw = self._raw or {}
        self.calls.append({"i": len(self.calls) + 1, "t_request": t0, "t_response": time.time(),
                           "model_version": getattr(resp, "model", None),
                           "raw_model_version": raw.get("modelVersion"), "raw_usage": raw.get("usageMetadata"),
                           "response_id": getattr(resp, "id", None), "created": getattr(resp, "created", None),
                           "system_fingerprint": getattr(resp, "system_fingerprint", None),
                           "usage": _dump(getattr(resp, "usage", None)), "cost": None, "pricing_status": None})
        try:
            self._write()
        except Exception as e:                       # review m3: never let a local write failure trigger a re-send
            raise CallLogWriteError(f"calls.json write failed after a billed response: {type(e).__name__}: {e}") from e
        return resp

    def _calculate_cost(self, response):
        rec = self.calls[-1] if self.calls and self.calls[-1]["response_id"] == getattr(response, "id", None) else None
        if self.registry is None:                       # legacy path (no registry): mini's own behaviour
            out = super()._calculate_cost(response)
            if rec is not None and rec["cost"] is None:
                rec["cost"] = out["cost"]; self._write()
            return out
        try:
            lc = super()._calculate_cost(response)["cost"]
        except Exception:
            lc = None
        status, rc = price_status(self.registry, getattr(response, "model", None),
                                  _dump(getattr(response, "usage", None)), lc)
        if rec is not None:
            rec.update({"pricing_status": status, "cost_litellm": lc, "cost_registry": rc})
            if status == "OK":
                rec["cost"] = lc
            else:
                rec["cost"] = None
                rec["provisional_usd"] = float(self.registry["provisional_usd_per_unpriced_call"])
            self._write()
        if status != "OK":
            self.pricing_failure = status
            raise PricingUnavailable(f"pricing {status}: served={getattr(response, 'model', None)!r}")
        return {"cost": lc}


def summarize(calls):
    """Trajectory-level sums from the call log: distinct versions (in order of first appearance), raw versions,
    token counts, known cost, provisional reservations for unpriced calls."""
    versions, raw, tok = [], [], {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    for c in calls:
        v = c.get("model_version")
        if v not in versions:
            versions.append(v)
        rv = c.get("raw_model_version")
        if rv is not None and rv not in raw:
            raw.append(rv)
        u = c.get("usage") or {}
        tok["prompt"] += u.get("prompt_tokens") or 0
        tok["completion"] += u.get("completion_tokens") or 0
        tok["reasoning"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        tok["cached"] += (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    unpriced = [c for c in calls if c.get("cost") is None]
    return {"model_versions": versions, "raw_model_versions": raw, "n_api_responses": len(calls), "tokens": tok,
            "api_cost_usd": sum(c.get("cost") or 0.0 for c in calls),
            "unpriced_calls": len(unpriced),
            "provisional_usd": sum(c.get("provisional_usd") or 0.0 for c in unpriced),
            "pricing_statuses": sorted({c.get("pricing_status") for c in calls if c.get("pricing_status")})}
