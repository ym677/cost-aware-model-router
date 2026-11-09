"""A bounded model router with explicit price assumptions and circuit state."""
from __future__ import annotations

import math
import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SPEC = {
    "title": "RouteCraft · 成本感知模型路由",
    "subtitle": "复杂度、预算约束、失败回退与熔断状态组成可观察的调用链",
    "slug": "cost-aware-model-router",
    "accent": "#7c3aed",
    "input_label": "可选首行填写“预算USD=0.002”，下一行输入问题",
    "sample_input": "预算USD=0.002\n比较预测性维护与定期维护，并给出多步分析和验证方案。",
}


def parse_input(text):
    match = re.match(r"\s*预算USD\s*=\s*([^\r\n]+)\r?\n", text, re.IGNORECASE)
    if match:
        try:
            budget = float(match.group(1))
        except ValueError as exc:
            raise ValueError("预算需填写有限的正数。") from exc
        query = text[match.end():].strip()
    else:
        if re.match(r"\s*预算USD", text, re.IGNORECASE):
            raise ValueError("预算格式为首行“预算USD=正数”，下一行填写问题。")
        budget, query = None, text.strip()
    if not query or (budget is not None and (not math.isfinite(budget) or budget <= 0)):
        raise ValueError("请填写问题，预算需大于 0。")
    return budget, query


def complexity(query):
    signals = {"长问题": len(query) > 120,
               "比较或权衡": bool(re.search(r"比较|权衡|对比|compare|trade.?off", query, re.I)),
               "推导或多步": bool(re.search(r"推导|多步|证明|方案|reason|proof", query, re.I)),
               "代码或结构": bool(re.search(r"代码|SQL|算法|```|code", query, re.I))}
    return min(1.0, 0.12 + sum(signals.values()) * 0.25), signals


def estimate_cost(query, input_price, output_price, output_limit=400):
    if input_price is None or output_price is None:
        return None
    # Character estimate includes a fixed prompt allowance, not a tokenizer count.
    estimated_input = math.ceil(len(query) / 2) + 100
    return (estimated_input * input_price + output_limit * output_price) / 1_000_000


def optional_price(settings, key):
    value = settings.get(key)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} 必须为非负美元费率。") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{key} 必须为有限非负数。")
    return number


class CircuitStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS circuits "
                               "(model TEXT PRIMARY KEY, failures INTEGER, until_time REAL)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def available(self, model, now=None):
        now = time.time() if now is None else now
        with self.connect() as connection:
            row = connection.execute("SELECT failures, until_time FROM circuits WHERE model=?", (model,)).fetchone()
        return row is None or row[1] <= now

    def record(self, model, success, now=None):
        now = time.time() if now is None else now
        with self.connect() as connection:
            row = connection.execute("SELECT failures, until_time FROM circuits WHERE model=?", (model,)).fetchone()
            failures = 0 if success else (row[0] if row else 0) + 1
            # A successful request resets failures. Two consecutive failures open for 60 s.
            until_time = now + 60 if failures >= 2 else 0
            connection.execute("INSERT OR REPLACE INTO circuits VALUES (?, ?, ?)",
                               (model, failures, until_time))


def plan_route(query, profiles, budget, circuit):
    score, signals = complexity(query)
    preferred = "strong" if score >= 0.5 else "cheap"
    order = [preferred, "cheap" if preferred == "strong" else "strong"]
    eligible = []
    trace = []
    for role in order:
        profile = profiles[role]
        cost = estimate_cost(query, profile["input_price"], profile["output_price"])
        if not circuit.available(profile["model"]):
            trace.append(f"{role} 暂处熔断窗口，跳过")
        elif budget is not None and cost is None:
            trace.append(f"{role} 缺少费率，不能按预算选择")
        elif budget is not None and cost > budget:
            trace.append(f"{role} 估算 ${cost:.6f} 超过单次预算 ${budget:.6f}")
        elif not any(item["model"] == profile["model"] for item in eligible):
            eligible.append({**profile, "role": role, "estimated_cost": cost})
    if not eligible:
        raise ValueError("没有满足预算且可用的模型。请核对费率、预算或等待熔断窗口结束。")
    return eligible, score, signals, trace


def execute_route(query, routes, client, circuit, budget=None):
    trace = []
    reserved_cost = 0.0
    for profile in routes:
        estimate = profile["estimated_cost"]
        if budget is not None:
            if estimate is None or reserved_cost + estimate > budget:
                trace.append(f"{profile['role']} 回退超出剩余估算预算，跳过")
                continue
            reserved_cost += estimate
        try:
            answer = client.complete(
                "你是技术分析助手。给出清晰可核验的分析，缺少信息时说明所需条件。",
                query, model=profile["model"], max_tokens=400,
            )
        except RuntimeError:
            circuit.record(profile["model"], success=False)
            trace.append(f"{profile['role']} 请求失败，已记录熔断计数")
            continue
        circuit.record(profile["model"], success=True)
        trace.append(f"{profile['role']} 实际请求成功")
        return answer, profile, trace
    raise ValueError("本次可用模型均未完成请求。失败已记录，未产生可展示的模型回答。")


def run(payload, client):
    budget, query = parse_input(payload["text"])
    settings = client.settings
    if client.enabled:
        profiles = {
            role: {"model": settings.get(f"ROUTER_{role.upper()}_MODEL") or settings["LLM_MODEL"],
                   "input_price": optional_price(settings, f"ROUTER_{role.upper()}_INPUT_PER_1M"),
                   "output_price": optional_price(settings, f"ROUTER_{role.upper()}_OUTPUT_PER_1M")}
            for role in ("cheap", "strong")
        }
    else:
        profiles = {
            "cheap": {"model": "demo-economy", "input_price": 0.5, "output_price": 2.0},
            "strong": {"model": "demo-reasoning", "input_price": 3.0, "output_price": 12.0},
        }
    mode = "live" if client.enabled else "demo"
    provider_hash = hashlib.sha256(settings.get("LLM_BASE_URL", "demo").encode()).hexdigest()[:12]
    circuit = CircuitStore(Path(__file__).parent / ".cache" / f"circuits-{mode}-{provider_hash}.sqlite3")
    routes, score, signals, trace = plan_route(query, profiles, budget, circuit)
    selected = routes[0]
    if client.enabled:
        answer, selected, request_trace = execute_route(query, routes, client, circuit, budget)
        trace += request_trace
    else:
        answer = f"本地路由演示选择 {selected['role']} 档。请求未发送到模型，以下展示选择依据和回退路径。"
        trace.append("演示模式只运行路由算法，没有模拟云端返回或延迟")
    rows = []
    for role, profile in profiles.items():
        cost = estimate_cost(query, profile["input_price"], profile["output_price"])
        rows.append([role, profile["model"], "未知" if cost is None else f"${cost:.6f}",
                     "可用" if circuit.available(profile["model"]) else "熔断"])
    return {
        "summary": answer,
        "metrics": {"选择模型": selected["model"], "复杂度启发分": score,
                    "单次预算USD": "未限定" if budget is None else budget,
                    "估算费用USD": "费率未配置" if selected["estimated_cost"] is None else round(selected["estimated_cost"], 6)},
        "sections": [
            {"title": "候选模型", "table": {"columns": ["档位", "模型", "请求前估算", "熔断状态"], "rows": rows}},
            {"title": "复杂度信号", "items": [f"{name}：{'命中' if value else '未命中'}" for name, value in signals.items()]},
            {"title": "调用与回退轨迹", "items": trace + ["候选顺序 " + " → ".join(route["role"] for route in routes)]},
            {"title": "计费说明", "text": "演示费率是假设数据。云端费率由用户配置，字符推算输入长度，输出按400 token预算估算。平台tokenizer、推理token、缓存和实时计费差异会影响实际账单。请求日志中的时延与usage来自真实响应。"},
        ],
    }
