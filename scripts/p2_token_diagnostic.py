"""Read-only pairing of frozen P2 request views, ledger rows, and provider usage.

Only the chosen output directory is written. This is an offline diagnostic, not
a tokenizer qualification or a request admission decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
from importlib import metadata
from pathlib import Path, PureWindowsPath

from tracefix.messages import Message
from tracefix.models.base import LLMConfig
from tracefix.models.input_bounds import count_deepseek_v41_request
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.tools.base import ToolSpec


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_frozen_path(
    raw: str, *, mounted_root: Path | None, windows_root: PureWindowsPath | None,
) -> Path:
    """Resolve an old Windows path inside a read-only container mount."""
    if mounted_root is None:
        return Path(raw)
    root = mounted_root.resolve(strict=True)
    windows = PureWindowsPath(raw)
    if windows.is_absolute():
        if windows_root is None or windows.drive.casefold() != windows_root.drive.casefold():
            raise ValueError(f"unmapped frozen path: {raw}")
        source_parts = tuple(part.casefold() for part in windows.parts)
        root_parts = tuple(part.casefold() for part in windows_root.parts)
        if source_parts[:len(root_parts)] != root_parts:
            raise ValueError(f"frozen path outside mapped root: {raw}")
        relative = windows.parts[len(root_parts):]
        candidate = root.joinpath(*relative)
    elif Path(raw).is_absolute():
        candidate = Path(raw)
    else:
        candidate = root.joinpath(*windows.parts)
    if ".." in candidate.parts:
        raise ValueError(f"frozen path traversal: {raw}")
    result = candidate.resolve(strict=True)
    if not result.is_relative_to(root) or not result.is_file():
        raise ValueError(f"frozen path missing or outside mapped root: {raw}")
    return result


def run(
    experiment: Path, ledger_path: Path, output: Path, *, official: bool = False,
    mounted_root: Path | None = None, windows_root: PureWindowsPath | None = None,
) -> Path:
    experiment = experiment.resolve()
    ledger_path = ledger_path.resolve()
    output = output.resolve()
    if output == experiment or output == ledger_path.parent or experiment in output.parents:
        raise ValueError("diagnostic output must be separate from frozen evidence")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol.json").read_text(encoding="utf-8"))
    trials = sorted((experiment / "trials").glob("[0-9][0-9][0-9].json"))
    if not trials:
        raise ValueError("no frozen trial records found")
    requests = {row["request_id"]: row for row in ledger["requests"]}
    adapter = LiteLLMAdapter(LLMConfig(model_name="deepseek/deepseek-flash"))
    rows: list[dict] = []
    trajectory_hashes: dict[str, str] = {}
    for trial_path in trials:
        trial = json.loads(trial_path.read_text(encoding="utf-8"))
        trajectory = resolve_frozen_path(
            trial["run_result_path"], mounted_root=mounted_root, windows_root=windows_root
        ).with_name("trajectory.jsonl")
        if not trajectory.is_file():
            raise ValueError(f"frozen trajectory missing: {trajectory}")
        trajectory_hashes[str(trajectory)] = sha256(trajectory)
        step = 0
        for line in trajectory.open(encoding="utf-8"):
            event = json.loads(line)
            if event.get("event_type") != "model_request_view":
                continue
            step += 1
            request_id = f"{trial['attempt_id']}:{step}"
            payload = event["payload"]
            messages = [Message.model_validate(item) for item in payload["messages"]]
            tools = [ToolSpec.model_validate(item) for item in payload.get("tools", [])]
            old_bound = adapter.count_input_tokens(messages, tools)
            formatted_messages = [adapter._format_message(message) for message in messages]
            formatted_tools = [tool.to_openai_tool() for tool in tools]
            request_body = {
                "model": "deepseek-flash",
                "messages": formatted_messages,
                "tools": formatted_tools,
                "thinking": {"type": "disabled"},
                "temperature": 0,
                "max_tokens": protocol["budgets"]["per_request_output_tokens"],
            }
            saved = requests.get(request_id)
            row = {
                "request_id": request_id,
                "old_byte_bound": old_bound,
                "ledger_bound": saved.get("input_token_upper_bound") if saved else None,
                "status": saved.get("status") if saved else "not_sent",
                "provider_input_tokens": saved.get("actual_input_tokens") if saved else None,
                "response_evidence_sha256": saved.get("response_evidence_sha256")
                if saved
                else None,
            }
            if saved and old_bound != saved["input_token_upper_bound"]:
                row["issue"] = "legacy_bound_mismatch"
            if official:
                serialized = json.dumps(
                    request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                bound = count_deepseek_v41_request(request_body)
                row["full_request_sha256"] = hashlib.sha256(serialized).hexdigest()
                row["official_count"] = {
                    "tokens": bound.tokens,
                    "status": bound.status,
                    "method": bound.method,
                    "identity": bound.identity,
                    "difference_from_provider_input": (
                        bound.tokens - saved["actual_input_tokens"]
                        if bound.tokens is not None
                        and saved
                        and isinstance(saved.get("actual_input_tokens"), int)
                        else None
                    ),
                }
            if saved and saved.get("response_evidence_path"):
                evidence = resolve_frozen_path(
                    saved["response_evidence_path"],
                    mounted_root=mounted_root, windows_root=windows_root,
                ) if mounted_root is not None else Path(saved["response_evidence_path"])
                if mounted_root is None and not evidence.is_absolute():
                    evidence = ledger_path.parent / evidence
                row["response_hash_matches"] = evidence.is_file() and sha256(evidence) == saved.get(
                    "response_evidence_sha256"
                )
                if row["response_hash_matches"]:
                    response = json.loads(evidence.read_text(encoding="utf-8"))
                    usage = response.get("usage") or {}
                    provider_usage = response.get("provider_usage") or {}
                    row["usage_matches"] = usage.get("input_tokens") == provider_usage.get(
                        "prompt_tokens"
                    ) == saved.get("actual_input_tokens") and usage.get(
                        "output_tokens"
                    ) == provider_usage.get("completion_tokens") == saved.get(
                        "actual_output_tokens"
                    )
            rows.append(row)
    ratios = sorted(
        row["old_byte_bound"] / row["provider_input_tokens"]
        for row in rows
        if isinstance(row["provider_input_tokens"], int) and row["provider_input_tokens"] > 0
    )
    report = {
        "kind": "p2_offline_legacy_byte_calibration",
        "experiment_protocol_sha256": sha256(experiment / "protocol.json"),
        "ledger_sha256": sha256(ledger_path),
        "trajectory_sha256": trajectory_hashes,
        "view_count": len(rows),
        "matched_ledger_count": sum(row["ledger_bound"] is not None for row in rows),
        "unsent_view_count": sum(row["status"] == "not_sent" for row in rows),
        "usage_pair_count": len(ratios),
        "legacy_bound_mismatch_count": sum(
            row.get("issue") == "legacy_bound_mismatch" for row in rows
        ),
        "response_hash_mismatch_count": sum(
            row.get("response_hash_matches") is False for row in rows
        ),
        "provider_usage_mismatch_count": sum(row.get("usage_matches") is False for row in rows),
        "ratio_old_bound_over_provider_input": {
            "minimum": min(ratios),
            "median": statistics.median(ratios),
            "maximum": max(ratios),
        }
        if ratios
        else None,
        "rows": rows,
        "interpretation": "Byte ratios are descriptive only; unsent views have no provider usage.",
    }
    if official:
        try:
            package_version = metadata.version("deepseek-recipe")
        except metadata.PackageNotFoundError:
            package_version = None
        tokenizer_setting = os.environ.get("TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON")
        tokenizer = Path(tokenizer_setting) if tokenizer_setting else None
        report["official_counter_identity"] = {
            "package_version": package_version,
            "tokenizer_sha256": sha256(tokenizer) if tokenizer and tokenizer.is_file() else None,
        }
        statuses: dict[str, int] = {}
        for row in rows:
            status = row["official_count"]["status"]
            statuses[status] = statuses.get(status, 0) + 1
        report["official_count_statuses"] = statuses
        report["official_scope"] = (
            "Full text Chat Completions body reconstructed from frozen messages and tool schemas; "
            "unsent requests have no provider usage. Counts remain estimates "
            "unless separately qualified."
        )
    output.mkdir(parents=True, exist_ok=False)
    destination = output / "token-calibration.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--mounted-root", type=Path)
    parser.add_argument("--windows-root")
    arguments = parser.parse_args()
    if bool(arguments.mounted_root) != bool(arguments.windows_root):
        parser.error("--mounted-root and --windows-root must be set together")
    print(
        run(
            arguments.experiment, arguments.ledger, arguments.output,
            official=arguments.official, mounted_root=arguments.mounted_root,
            windows_root=PureWindowsPath(arguments.windows_root)
            if arguments.windows_root else None,
        )
    )
