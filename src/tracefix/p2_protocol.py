"""P2 真实任务 C/T 实验的冻结协议与零费用演练。"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.exceptions import BenchmarkError
from tracefix.paired import ExperimentArm
from tracefix.real_benchmark import RealIssueTask, load_real_issue_tasks
from tracefix.real_recipes import load_environment_recipes

P1_QUALIFIED_TASK_IDS = (
    "psf__requests-1142", "psf__requests-1766", "pylint-dev__pylint-4551",
    "pylint-dev__pylint-4604", "pylint-dev__pylint-4661", "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081", "pytest-dev__pytest-10356", "sphinx-doc__sphinx-10435",
    "sphinx-doc__sphinx-10449",
)
COLLECTION_FAILURE_TASK_IDS = ("pylint-dev__pylint-4551", "pylint-dev__pylint-4604")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        raise BenchmarkError("cannot determine P2 code commit")
    return completed.stdout.strip()


class P2FormalRunRequirements(BaseModel):
    """正式调用前必须由操作者填写且可审计的商业参数。"""

    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    pricing_source: str = Field(min_length=1)
    total_cost_cap_usd: float = Field(gt=0)


class P2ProtocolConfig(BaseModel):
    """固定整体优化 C/T 设计；离线模式从不创建供应商客户端。"""

    model_config = ConfigDict(extra="forbid")

    tasks_dir: Path = Path("benchmarks/real_candidates")
    recipes_dir: Path = Path("benchmarks/real_recipes")
    source_root: Path = Path("runs/real-candidate-validation-v080b")
    test_env_root: Path = Path("runs/p1-revalidation-20260917/environments")
    output_dir: Path = Path("runs")
    repetitions: int = Field(default=3, ge=3, le=3)
    context_trigger_tokens: int = Field(default=32_000, ge=1)
    max_input_tokens: int = Field(default=350_000, ge=1)
    max_output_tokens: int = Field(default=20_000, ge=1)
    max_steps: int = Field(default=24, ge=1)
    max_test_runs: int = Field(default=6, ge=1)
    wall_time_seconds: int = Field(default=900, ge=1)
    per_request_output_tokens: int = Field(default=4_096, ge=1)
    formal: P2FormalRunRequirements | None = None

    @model_validator(mode="after")
    def validate_design(self) -> P2ProtocolConfig:
        if self.repetitions != 3:
            raise ValueError("P2 fixes exactly three repetitions per arm")
        return self


class P2TrialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    task_id: str
    repetition: int = Field(ge=1, le=3)
    arm: ExperimentArm
    token_optimization_enabled: bool
    repo_map_enabled: bool
    context_compaction_enabled: bool


class P2ProtocolRecord(BaseModel):
    """可提交的脱敏协议，包含可重建运行的全部身份信息。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = "p2_whole_system_ct_protocol"
    generated_at: datetime
    code_commit: str
    offline_only: bool
    formal_ready: bool
    formal_missing: tuple[str, ...]
    qualified_task_ids: tuple[str, ...]
    primary_task_ids: tuple[str, ...]
    collection_failure_task_ids: tuple[str, ...]
    task_hashes: dict[str, dict[str, str]]
    recipe_hashes: dict[str, str]
    budgets: dict[str, int]
    arm_configurations: dict[str, dict[str, bool | int]]
    schedule: tuple[P2TrialPlan, ...]
    formal: P2FormalRunRequirements | None = None


def _task_hashes(tasks: tuple[RealIssueTask, ...]) -> dict[str, dict[str, str]]:
    return {
        task.id: {
            "base_commit": task.base_commit,
            "problem_statement": task.hashes.problem_statement,
            "gold_patch": task.hashes.gold_patch,
            "test_patch": task.hashes.test_patch,
        }
        for task in tasks
    }


def _schedule(task_ids: tuple[str, ...]) -> tuple[P2TrialPlan, ...]:
    plans: list[P2TrialPlan] = []
    for repetition, arms in enumerate(
        ((ExperimentArm.CONTROL, ExperimentArm.TREATMENT),
         (ExperimentArm.TREATMENT, ExperimentArm.CONTROL),
         (ExperimentArm.CONTROL, ExperimentArm.TREATMENT)), start=1
    ):
        for task_id in task_ids:
            for arm in arms:
                enabled = arm is ExperimentArm.TREATMENT
                plans.append(P2TrialPlan(
                    sequence=len(plans) + 1, task_id=task_id, repetition=repetition, arm=arm,
                    token_optimization_enabled=enabled, repo_map_enabled=enabled,
                    context_compaction_enabled=enabled,
                ))
    return tuple(plans)


def build_p2_protocol(
    config: P2ProtocolConfig, *, repository_root: Path = Path(".")
) -> P2ProtocolRecord:
    """验证 P1 入选集，冻结输入，并构造唯一的 60 次试验顺序。"""
    tasks = load_real_issue_tasks(config.tasks_dir, task_ids=P1_QUALIFIED_TASK_IDS)
    ids = tuple(task.id for task in tasks)
    if ids != tuple(sorted(P1_QUALIFIED_TASK_IDS)):
        raise BenchmarkError("P2 qualified task set is incomplete or changed")
    recipes = load_environment_recipes(config.recipes_dir)
    missing_recipes = sorted(set(ids) - set(recipes))
    if missing_recipes:
        raise BenchmarkError(
            "P2 is missing environment recipes", context={"task_ids": missing_recipes}
        )
    missing = (
        ()
        if config.formal
        else ("model_name", "provider", "pricing_source", "total_cost_cap_usd")
    )
    return P2ProtocolRecord(
        generated_at=datetime.now(UTC),
        code_commit=_git_commit(repository_root),
        offline_only=config.formal is None,
        formal_ready=config.formal is not None,
        formal_missing=missing,
        qualified_task_ids=ids,
        primary_task_ids=tuple(item for item in ids if item not in COLLECTION_FAILURE_TASK_IDS),
        collection_failure_task_ids=COLLECTION_FAILURE_TASK_IDS,
        task_hashes=_task_hashes(tasks),
        recipe_hashes={key: recipes[key].fingerprint for key in ids},
        budgets={
            "max_input_tokens": config.max_input_tokens,
            "max_output_tokens": config.max_output_tokens,
            "max_steps": config.max_steps,
            "max_test_runs": config.max_test_runs,
            "wall_time_seconds": config.wall_time_seconds,
            "per_request_output_tokens": config.per_request_output_tokens,
        },
        arm_configurations={
            "control": {
                "token_optimization_enabled": False,
                "repo_map_enabled": False,
                "context_compaction_enabled": False,
                "context_trigger_tokens": config.context_trigger_tokens,
            },
            "treatment": {
                "token_optimization_enabled": True,
                "repo_map_enabled": True,
                "context_compaction_enabled": True,
                "context_trigger_tokens": config.context_trigger_tokens,
            },
        },
        schedule=_schedule(ids),
        formal=config.formal,
    )


def write_p2_dry_run(config: P2ProtocolConfig, *, repository_root: Path = Path(".")) -> Path:
    """落盘零费用演练记录；此函数不初始化 LLM 或读取任何密钥。"""
    protocol = build_p2_protocol(config, repository_root=repository_root)
    root = config.output_dir.expanduser().resolve() / "p2-dry-run"
    root.mkdir(parents=True, exist_ok=False)
    path = root / "p2-protocol.json"
    path.write_text(protocol.model_dump_json(indent=2), encoding="utf-8")
    return path
