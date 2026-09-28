"""Render a saved TraceFix run as a portable, offline HTML report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from tracefix.exceptions import sanitize_payload


def _read_run(path: Path) -> Path:
    root = path.expanduser().resolve()
    if (root / "result.json").is_file():
        return root
    runs = root / "runs"
    candidates = (
        [item for item in runs.iterdir() if (item / "result.json").is_file()]
        if runs.is_dir()
        else []
    )
    if len(candidates) != 1:
        raise ValueError(
            "--run must contain exactly one saved run; select its runs/<run-id> directory"
        )
    return candidates[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read required evidence {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def _read_events(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read required evidence {path.name}: {exc}") from exc
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                warnings.append("轨迹末尾不完整；最后一个事件未纳入报告。")
                break
            raise ValueError(f"invalid trajectory event on line {index + 1}: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"invalid trajectory event on line {index + 1}")
        events.append(event)
    return events, warnings


def _clean(value: Any) -> str:
    sanitized = sanitize_payload(value)
    raw = (
        sanitized
        if isinstance(sanitized, str)
        else json.dumps(sanitized, ensure_ascii=False, indent=2)
    )
    raw = re.sub(r"(?i)\b[A-Z]:[\\/]+[^\s\"'<>]+", "<本机路径>", raw)
    raw = re.sub(r"/(?:home/runner|Users|tmp)/[^\s\"'<>]+", "<本机路径>", raw)
    return html.escape(raw, quote=True)


def _label(value: Any) -> str:
    return _clean(value) if value is not None else "未记录"


def _diff(patch: str) -> str:
    rows = []
    for line in patch.splitlines():
        css = (
            "add"
            if line.startswith("+") and not line.startswith("+++")
            else ("remove" if line.startswith("-") and not line.startswith("---") else "context")
        )
        rows.append(f'<span class="{css}">{_clean(line)}</span>')
    return '<pre class="diff">' + "\n".join(rows) + "</pre>" if rows else "<p>没有保存补丁。</p>"


def _test_status(result: dict[str, Any]) -> str:
    output = result.get("output") or {}
    if not isinstance(output, dict):
        return "证据格式异常"
    code = output.get("returncode")
    if code == 0 and result.get("success") is True:
        return "公开测试通过"
    if output.get("timed_out"):
        return "公开测试超时"
    if code is None:
        return "公开测试未得到退出码"
    return f"公开测试失败（退出码 {code}）"


def render_report(run: Path, output: Path | None = None) -> Path:
    """Read local evidence only, then render one standalone HTML file."""
    run_dir = _read_run(run)
    result_path = run_dir / "result.json"
    trace_path = run_dir / "trajectory.jsonl"
    result = _read_json(result_path)
    events, warnings = _read_events(trace_path)
    patch_path = run_dir / "patch.diff"
    patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
    if not patch_path.is_file():
        warnings.append("缺少 patch.diff；补丁区域不完整。")
    reproduction_path = run_dir.parent.parent / "reproduction.json"
    reproduction = _read_json(reproduction_path) if reproduction_path.is_file() else {}

    calls: dict[str, dict[str, Any]] = {}
    timeline: list[tuple[str, str, Any]] = []
    tests: list[str] = []
    model_requests = 0
    skills: list[str] = []
    compacted = 0
    task = "未记录"
    for event in events:
        kind = event.get("event_type")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if kind == "task_started":
            task = str(payload.get("task") or task)
        elif kind == "model_requested":
            model_requests += 1
        elif kind == "context_compacted":
            compacted += 1
        elif kind == "skill_activated":
            skills.append(str(payload.get("name") or payload.get("skill_name") or "技能"))
            timeline.append(("技能加载", skills[-1], payload))
        elif kind == "tool_called":
            call = payload.get("call") or {}
            if isinstance(call, dict):
                call_id = str(call.get("id") or "")
                calls[call_id] = call
                timeline.append(("工具调用", str(call.get("name") or "未知工具"), call))
        elif kind == "tool_returned":
            tool = payload.get("result") or {}
            if isinstance(tool, dict):
                call_id = str(tool.get("call_id") or "")
                name = str(
                    tool.get("tool_name") or calls.get(call_id, {}).get("name") or "未知工具"
                )
                title = _test_status(tool) if name == "run_tests" else name
                timeline.append(("工具结果", title, tool))
                if name == "run_tests":
                    tests.append(title)
                if call_id not in calls:
                    warnings.append(f"工具 {name} 有返回但没有配对调用。")
                else:
                    calls.pop(call_id)
        elif kind == "error":
            timeline.append(("错误", "运行错误", payload))
    if calls:
        warnings.append(f"{len(calls)} 个工具调用没有已保存的返回结果；执行状态未知。")

    offline = result.get("model_name") == "offline/scripted"
    mode = "脚本模型演示 · 无供应商调用" if offline else "已保存的模型运行回放"
    if offline and reproduction.get("provider_request_attempts") != 0:
        warnings.append("离线模式的供应商请求证据缺失或不为零。")
    result_status = str(result.get("status") or "未知")
    validation = str(result.get("agent_validation_status") or "unverified")
    time_value = result.get("duration_seconds")
    time_text = f"{time_value:.2f} 秒" if isinstance(time_value, (float, int)) else "未记录"
    cost = (
        "脚本运行，不适用"
        if offline
        else f"${result.get('cost_usd', 0):.6f}"
        if result.get("cost_complete") is True
        else "未记录"
    )
    usage = (
        "脚本运行，不适用"
        if offline
        else (
            f"输入 {_label(result.get('input_tokens'))} / "
            f"输出 {_label(result.get('output_tokens'))}"
        )
    )
    context = result.get("context_metrics") or {}
    context_saved = context.get("estimated_tokens_saved") if isinstance(context, dict) else None
    context_text = (
        f"估算节省 {context_saved} Token"
        if isinstance(context_saved, int) and not offline
        else "脚本模型不提供可信 Token 估算"
        if offline
        else "未记录"
    )
    rows = "".join(
        f'<li><span class="dot"></span><div><strong>{_clean(title)}</strong>'
        f"<small>{_clean(kind)}</small><details><summary>查看原始记录</summary>"
        f"<pre>{_clean(payload)}</pre></details></div></li>"
        for kind, title, payload in timeline
    )
    warning_html = (
        "".join(f"<li>{_clean(item)}</li>" for item in warnings) or "<li>未发现报告证据缺口。</li>"
    )
    test_html = (
        "".join(f"<li>{_clean(item)}</li>" for item in tests) or "<li>没有保存公开测试调用。</li>"
    )
    identities = "".join(
        f"<li>{name}: {_clean(hashlib.sha256(path.read_bytes()).hexdigest())}</li>"
        for name, path in (
            ("result.json", result_path),
            ("trajectory.jsonl", trace_path),
            ("patch.diff", patch_path),
        )
        if path.is_file()
    )
    content = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TraceFix 修复报告</title><style>
:root{{--ink:#172334;--muted:#576777;--line:#dae2e9;--paper:#f5f7fa;--accent:#086f70}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,sans-serif}}
main{{max-width:1050px;margin:auto;padding:38px 24px 70px}}
header{{background:#132d3a;color:#fff;padding:34px;border-radius:20px}}
h1{{font-size:2rem;margin:.1em 0}}h2{{margin:0 0 14px;font-size:1.35rem}}header p{{max-width:70ch}}
.eyebrow{{color:#9ee1d1;font-weight:700;letter-spacing:.1em}}
.badge{{display:inline-block;background:#d4faf1;color:#075957;padding:4px 12px;
border-radius:20px;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin:18px 0}}
.card,section{{background:#fff;border:1px solid var(--line);
border-radius:16px;padding:22px;margin:18px 0}}
.card b{{display:block;font-size:1.35rem}}.card small,small,.muted{{color:var(--muted)}}
ol.timeline{{list-style:none;padding:0;margin:0}}
.timeline li{{display:flex;gap:16px;padding:0 0 18px;
border-left:2px solid #b6dad7;margin-left:7px}}
.timeline li:last-child{{border:0}}
.dot{{width:14px;height:14px;border-radius:50%;background:var(--accent);
margin-left:-8px;flex:none}}
.timeline li>div{{padding:0 0 0 2px;min-width:0;width:100%}}
.timeline strong,.timeline small{{display:block}}
details{{margin-top:8px;border:1px solid var(--line);border-radius:8px;padding:7px 12px}}
summary{{cursor:pointer;color:var(--accent);font-weight:600}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,monospace;
max-height:440px;overflow:auto}}
.diff{{background:#15242d;color:#ecf5f4;padding:18px;border-radius:10px}}
.diff span{{display:block}}.diff .add{{color:#93e5b4}}.diff .remove{{color:#ffa59b}}
ul{{padding-left:20px}}footer{{color:var(--muted);font-size:13px}}
</style></head><body><main><header><div class="eyebrow">TRACEFIX · SAVED RUN</div>
<h1>从失败证据到可检查补丁</h1><p>{_clean(task)}</p>
<span class="badge">{_clean(mode)}</span></header>
<div class="grid"><div class="card"><small>运行状态</small><b>{_clean(result_status)}</b></div>
<div class="card"><small>Agent 验证</small><b>{_clean(validation)}</b></div>
<div class="card"><small>耗时</small><b>{_clean(time_text)}</b></div>
<div class="card"><small>模型请求 / 工具返回</small>
<b>{model_requests} / {sum(1 for k, _, _ in timeline if k == "工具结果")}</b></div></div>
<section><h2>运行概览</h2><p>模型：{_label(result.get("model_name"))} ·
后端：{_label(reproduction.get("execution_backend"))} ·
源码提交：{_label(result.get("source_commit"))}</p>
<p>停止原因：{_label(result.get("stop_reason"))}</p>
<p>Agent 结束表示控制流结束；公开测试结果和独立验收分别列示，
不由结束状态推断修复成功。</p></section>
<section><h2>修复时间线</h2><ol class="timeline">{rows}</ol></section>
<section><h2>测试与补丁</h2><ul>{test_html}</ul><p>独立验收：未记录</p>{_diff(patch)}</section>
<section><h2>资源与 Skills</h2>
<p>模型 usage：{usage} · 费用：{cost} · 上下文折叠：{compacted} 次</p>
<p>上下文成本：{context_text}</p>
<p>已加载技能：{_clean(", ".join(skills) if skills else "无")}</p></section>
<section><h2>证据来源与限制</h2><ul>{warning_html}</ul>
<details><summary>输入文件 SHA-256</summary><ul>{identities}</ul></details>
<p class="muted">本页根据已保存工件生成。日志和补丁可能包含业务源码，请检查后再公开。</p></section>
<footer>TraceFix · 离线报告 · 无外部资源</footer></main></body></html>"""
    target = (output or run_dir / "report.html").expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        print(render_report(args.run, args.output))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"TraceFix 报告错误: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
