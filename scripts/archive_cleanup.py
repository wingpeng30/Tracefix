"""One-off TraceFix archival cleanup. Prepare first; purge requires a remote Git gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(r"D:\Tracefix")
ARCHIVE = Path(r"E:\TracefixExperiments\interview-archive-20261006")
EXPERIMENTS = Path(r"E:\TracefixExperiments")
CURRENT = EXPERIMENTS / "20261006-abc-holdout" / "campaign"
NEW = EXPERIMENTS / "20261006-bc-holdout-180s" / "campaign"
EXTERNAL = [Path(r"E:\TraceFixRunsActive"), Path(r"E:\TraceFixRunsArchive")]
TEMP = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp"
IMPORTANT = {
    "agent-model-comparison-20261005",
    "bc-capability-20261006",
    "live-2026-09-29",
    "live-public-three-types-20261002",
}
GENERATED = re.compile(
    r"^(?:\.?pytest|\.?test[-_]|\.test-tmp|\.tfx|\.tf-|\.p2|\.docker-tmp|"
    r"\.d-(?:artifact|quality|pytest)|_pytest|portfolio-engineering|gate-|"
    r"recovery-fix|regression-suite|\d+(?:\.\d+)?$|\.coverage$|\.ruff_cache$)"
)
PRUNE = {
    ".git",
    ".venv",
    "venv",
    "Lib",
    "lib",
    "lib64",
    "site-packages",
    "Include",
    "Scripts",
    "bin",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "workspace",
    "checkout",
    "source",
    "sources",
    "source-tree",
    "repo",
    "environments",
    "envs",
    "node_modules",
    "build",
    "dist",
    "uv-cache",
    "uv-python",
}
SUFFIXES = {
    ".json",
    ".jsonl",
    ".xml",
    ".log",
    ".txt",
    ".md",
    ".diff",
    ".patch",
    ".html",
    ".zip",
    ".bundle",
    ".csv",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def key(path):
    return os.path.normcase(os.path.abspath(path))


def within(path, parent):
    p, base = key(path), key(parent)
    return p == base or p.startswith(base + os.sep)


def linked(info):
    return bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ) or stat.S_ISLNK(info.st_mode)


def no_link_ancestors(path):
    for part in [Path(path), *Path(path).parents]:
        if part.exists() and linked(part.lstat()):
            raise ValueError(f"reparse ancestor: {part}")


def snapshot(path):
    """Metadata identity for deletion concurrency checks; never follow reparse points."""
    path = Path(path)
    no_link_ancestors(path)
    h = hashlib.sha256()
    size, count = 0, 0
    stack = [(str(path), path.lstat())]
    while stack:
        current, info = stack.pop()
        if linked(info):
            raise ValueError(f"nested reparse point: {current}")
        relative = os.path.relpath(current, path)
        h.update(
            json.dumps(
                [relative, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode],
                ensure_ascii=False,
            ).encode()
        )
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                # Windows enumeration supplies timestamps and reparse attributes without
                # reopening every file. Creation time identifies replacement files.
                stack.extend(
                    sorted(
                        ((e.path, e.stat(follow_symlinks=False)) for e in entries),
                        key=lambda row: row[0],
                        reverse=True,
                    )
                )
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
            count += 1
        else:
            raise ValueError(f"non-regular artifact: {current}")
    return {"fingerprint": h.hexdigest(), "bytes": size, "files": count}


def protected_paths():
    protected = [ARCHIVE]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode().split("\0")
    protected.extend(REPO / name for name in tracked if name)
    protected.extend(
        REPO / name
        for name in (
            ".git",
            ".venv",
            ".env",
            "src",
            "tests",
            "scripts",
            "docs",
            "benchmarks",
            "examples",
            "requirements",
            "docker",
            ".github",
            "portfolio-demo-skills-2",
        )
    )
    # Generated test subdirectories are considered separately, with tracked files checked again.
    protected.remove(REPO / "tests")
    c_rows = [
        r
        for r in csv.DictReader((CURRENT / "per-run.csv").open(encoding="utf-8"))
        if r["arm"] == "C"
    ]
    protected.extend(CURRENT / "trials" / r["id"] for r in c_rows)
    protected.extend(CURRENT / name for name in ("provider", "prompts", "qualification", "blocks"))
    protected.extend(p for p in CURRENT.iterdir() if p.is_file())
    protected.extend(p for p in CURRENT.parent.iterdir() if p.is_file())
    protected.append(NEW)
    protected.extend(p for p in NEW.parent.iterdir() if p.is_file())
    for root in (CURRENT, NEW):
        protocol = read(root / "protocol.json")
        for task in protocol["tasks"].values():
            protected.extend([Path(task["source"]), Path(task["python"]).parent.parent])
            protected.append(Path(task["prompt_path"]).parent)
    protected.extend(
        REPO / "tmp" / name
        for name in (
            "bc-tokenizer-libs",
            "bc-v41-tokenizer.json",
            "abc-counter-python-cf8d175",
            "abc-httpbin-20261006.json",
        )
    )
    protected.append(REPO / "runs" / "holdout-httpbin-server-20260922")
    return sorted(set(protected), key=key), c_rows, tracked


def allowed_roots():
    roots = [
        REPO / "runs",
        REPO / "tmp",
        REPO / "tests",
        *EXTERNAL,
        CURRENT.parent,
        NEW.parent,
        TEMP,
    ]
    roots.extend(p for p in Path("D:/").glob("Tracefix-test-*") if p.is_dir())
    # Other user/system temporary directories are deliberately not inferred from their name.
    return roots


def guard(path, roots, protected):
    path = Path(path)
    if key(path) in {key(REPO), key(EXPERIMENTS), key(ARCHIVE), key(TEMP)}:
        raise ValueError("root deletion forbidden")
    if within(path, TEMP) and (
        path.parent != TEMP or not path.name.startswith("tracefix-test-evidence-")
    ):
        raise ValueError("unrelated temporary directory")
    if not any(within(path, root) for root in roots):
        if path.parent != REPO or not GENERATED.match(path.name):
            raise ValueError(f"outside authorized artifact roots: {path}")
    for keep in protected:
        if within(keep, path) or within(path, keep):
            raise ValueError(f"protected overlap: {keep}")
    no_link_ancestors(path)


def candidates(protected, tracked):
    result = []
    containers = [REPO / "runs", REPO / "tmp", *EXTERNAL, CURRENT.parent, NEW.parent]

    def visit(path):
        if any(within(path, p) for p in protected):
            return
        if any(within(p, path) for p in protected):
            if path.is_dir() and not linked(path.lstat()):
                for child in sorted(path.iterdir()):
                    visit(child)
            return
        result.append(path)

    for container in containers:
        for child in sorted(container.iterdir()):
            visit(child)
    for p in sorted(REPO.iterdir()):
        if GENERATED.match(p.name):
            visit(p)
    for p in (REPO / "tests").iterdir():
        if p.name.startswith((".tmp-workspace-", ".validation-gate-", "__pycache__")):
            visit(p)
    # These paths are used by TraceFix's evidence tool and are outside other apps' temp folders.
    for root in allowed_roots():
        if root.parent == Path("D:/"):
            visit(root)
    for root in TEMP.glob("tracefix-test-evidence-*"):
        if root.is_dir():
            visit(root)
    unique = sorted(set(result), key=key)
    names = {key(p) for p in unique}
    return [p for p in unique if not any(key(parent) in names for parent in p.parents)]


def archive_files(root, important=False, full=False, shallow=False, summary_only=False):
    root = Path(root)
    if root.is_file():
        yield root
        return
    stack = [root]
    representatives = set()
    if not important and not full:
        # The first trace in each observed status class; statistics keep all original positions.
        for directory, children, files in os.walk(root, followlinks=False):
            children[:] = sorted(
                n for n in children if n not in PRUNE and not linked(Path(directory, n).lstat())
            )
            if "result.json" in files or "record.json" in files:
                try:
                    name = "record.json" if "record.json" in files else "result.json"
                    value = read(Path(directory, name))
                    status = str(value.get("status", value.get("agent_status", "unknown")))
                    status += ":" + str(value.get("passed", value.get("resolved", "unmeasured")))
                    representatives.add((status, directory))
                except (OSError, ValueError):
                    pass
            if shallow:
                children[:] = []
        first = {}
        for status, directory in sorted(representatives, key=lambda r: r[1]):
            first.setdefault(status, directory)
        representatives = set(first.values())
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                info = entry.stat(follow_symlinks=False)
                if linked(info):
                    continue
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    if full or (
                        not shallow
                        and entry.name not in PRUNE
                        and not entry.name.endswith(".egg-info")
                    ):
                        # A nested checkout is not an evidence directory.
                        if full or not (path / ".git").exists():
                            stack.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if summary_only and path.name not in {
                        "record.json",
                        "result.json",
                        "summary.json",
                        "verification.json",
                    }:
                        continue
                    if full or path.suffix.lower() in SUFFIXES:
                        if not full and not important:
                            if (
                                "provider" in path.relative_to(root).parts
                                or "prompts" in path.relative_to(root).parts
                            ):
                                continue
                            if path.suffix == ".jsonl" and str(path.parent) not in representatives:
                                continue
                        yield path


def zip_archive(root, output, **options):
    output = Path(output)
    if output.exists():
        manifest = read(output.with_suffix(".manifest.json"))
        if sha(output) != manifest["zip_sha256"]:
            raise ValueError(f"existing archive corrupted: {output}")
        selected = {str(p) for p in archive_files(root, **options)}
        if selected != {row["source"] for row in manifest["files"]}:
            raise ValueError(f"archived source membership changed: {root}")
        for row in manifest["files"]:
            if not within(row["source"], root) or sha(row["source"]) != row["sha256"]:
                raise ValueError(f"archived source changed: {row['source']}")
        return manifest
    items = []
    temporary = output.with_suffix(".zip.writing")
    with zipfile.ZipFile(
        temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as z:
        for path in archive_files(root, **options):
            name = (
                str(path.relative_to(root)).replace("\\", "/") if Path(root).is_dir() else path.name
            )
            before = path.stat()
            hashed = sha(path)
            z.write(path, name)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f"artifact changed during archival: {path}")
            items.append(
                {"source": str(path), "member": name, "bytes": before.st_size, "sha256": hashed}
            )
    os.replace(temporary, output)
    manifest = {
        "source_root": str(root),
        "archive": str(output),
        "selection": options,
        "coverage": "Full tree"
        if options.get("full")
        else (
            "Core evidence subset; regenerated workspaces/environments omitted; "
            "old resume unsupported"
        ),
        "zip_sha256": sha(output),
        "files": items,
    }
    verify_zip(manifest)
    write(output.with_suffix(".manifest.json"), manifest)
    return manifest


def verify_zip(manifest):
    if sha(manifest["archive"]) != manifest["zip_sha256"]:
        raise ValueError("archive digest mismatch")
    with zipfile.ZipFile(manifest["archive"]) as z:
        if set(z.namelist()) != {r["member"] for r in manifest["files"]}:
            raise ValueError("archive membership mismatch")
        for row in manifest["files"]:
            h = hashlib.sha256()
            with z.open(row["member"]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != row["sha256"]:
                raise ValueError(f"archive member corrupted: {row['member']}")


def verify_c():
    protocol = read(CURRENT / "protocol.json")
    if digest(protocol) != read(CURRENT / "protocol.sha256.json")["sha256"]:
        raise ValueError("C parent protocol corrupted")
    ledger = read(CURRENT / "requests.json")
    if ledger["protocol_sha256"] != digest(protocol):
        raise ValueError("C ledger identity mismatch")
    for row in ledger["requests"]:
        if row["status"] != "completed":
            raise ValueError("unknown parent request")
        for suffix in ("request", "response"):
            if sha(CURRENT / "provider" / f"{row['id']}-{suffix}.json") != row[f"{suffix}_sha256"]:
                raise ValueError("funding provider artifact corrupted")
    rows = [
        r
        for r in csv.DictReader((CURRENT / "per-run.csv").open(encoding="utf-8"))
        if r["arm"] == "C"
    ]
    if len(rows) != 6:
        raise ValueError("unexpected C progress; replan before deleting")
    for row in rows:
        root = CURRENT / "trials" / row["id"]
        record = read(root / "record.json")
        unsigned = dict(record)
        expected = unsigned.pop("record_sha256")
        if digest(unsigned) != expected or record["arm"] != "C" or not record["finished"]:
            raise ValueError("C record identity mismatch")
        for relative, expected in record["artifacts"].items():
            path = root / relative
            if not within(path, root) or sha(path) != expected:
                raise ValueError(f"C artifact corrupted: {path}")
    amount = sum(r["peak_cost_cny"] for r in ledger["requests"])
    if abs(amount - 79.085016) > 0.000001:
        raise ValueError("parent spend changed")
    return {
        "c_trials": 6,
        "independent_passes": sum(r["independent_pass"] == "True" for r in rows),
        "parent_requests": len(ledger["requests"]),
        "parent_spend_cny": amount,
        "ledger_sha256": sha(CURRENT / "requests.json"),
        "protocol_sha256": sha(CURRENT / "protocol.json"),
    }


def free_space():
    return {drive: shutil.disk_usage(drive + ":\\").free for drive in ("C", "D", "E")}


def archive_catalog():
    entries = []
    for path in sorted(ARCHIVE.rglob("*")):
        if path.is_file() and path.suffix in {".zip", ".bundle"}:
            manifest_path = path.with_suffix(".manifest.json")
            manifest = read(manifest_path) if manifest_path.exists() else {}
            hashed = sha(path)
            if manifest and hashed != manifest["zip_sha256"]:
                raise ValueError(f"catalog archive mismatch: {path}")
            entries.append(
                {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": hashed,
                    "source_root": manifest.get("source_root"),
                    "manifest": str(manifest_path) if manifest else None,
                    "coverage": manifest.get("coverage", "Git code snapshot/history"),
                }
            )
    write(ARCHIVE / "archive-catalog.json", entries)
    sums = "".join(
        f"{row['sha256']}  {Path(row['path']).relative_to(ARCHIVE)}\n" for row in entries
    )
    (ARCHIVE / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    return {"archives": len(entries), "bytes": sum(row["bytes"] for row in entries)}


def manual_cleanup_report():
    report_path = ARCHIVE / "final-report.json"
    if not report_path.exists():
        report_path = ARCHIVE / "cleanup-plan.json"
    report = read(report_path)
    protected, c_rows, _ = protected_paths()
    targets = {}
    retained = []
    sizes = {key(row["path"]): row.get("bytes") for row in report["skipped"]}

    def add(path, reason):
        try:
            guard(path, allowed_roots(), [])
        except ValueError as error:
            if "reparse" not in str(error):
                retained.append(str(path) + "（不在允许的手动清理范围）")
                return
        except OSError:
            pass
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except PermissionError:
            info = None
        if any(within(path, keep) for keep in protected):
            retained.append(str(path))
            return
        if any(within(keep, path) for keep in protected):
            retained.append(str(path) + "（含 C 必要依赖，不可整目录删除）")
            try:
                if info and stat.S_ISDIR(info.st_mode) and not linked(info):
                    for child in path.iterdir():
                        add(child, "原父目录含 C 依赖；此子项尚未单独完成删除校验")
            except OSError:
                pass
            return
        if info and linked(info):
            reason += "；只能移除此链接自身，不要删除链接目标"
        targets[key(path)] = {"path": str(path), "reason": reason, "bytes": sizes.get(key(path))}

    for row in report["skipped"]:
        add(Path(row["path"]), row["reason"])
    rows = sorted(targets.values(), key=lambda row: key(row["path"]))
    write(
        ARCHIVE / "manual-cleanup.json",
        {
            "source_report": str(report_path),
            "targets": rows,
            "required_retained": retained,
        },
    )
    lines = [
        "# 需手动复核的 TraceFix 清理项",
        "",
        f"共 {len(rows)} 项。以下是建议精简的旧生成目录，但自动校验未通过。",
        "这不是无条件删除授权清单。",
        "",
        "权限项未强改 ACL，未能读全的内容不保证已经归档。手动删除前确认没有独有代码或面试材料。",
        "变化项先核对新文件与归档；链接项只移除链接自身，不跟随或删除目标。",
        "不要删除 C 的六次记录、共同 provider/费用账本、冻结源码、实际环境、",
        "原始 prompts 或归档目录。",
        "",
    ]
    if c_rows:
        lines.extend(["C 必须保留的目录编号：`" + "、".join(r["id"] for r in c_rows) + "`。", ""])
    ab = [row for row in rows if Path(row["path"]).parent == CURRENT / "trials"]
    if ab:
        lines.extend(["## 当前 A/B 残留", "", "对应结果摘要已保留；下列工作区仍需手动复核：", ""])
        lines.extend(f"- `{row['path']}`" for row in ab)
        lines.append("")
    biggest = sorted(rows, key=lambda row: row["bytes"] or 0, reverse=True)[:15]
    lines.extend(["## 大型副本", "", "仅列已有快照大小的较大项，大小是逻辑值。", ""])
    lines.extend(f"- `{r['path']}`：{r['bytes'] / 1024**3:.2f} GiB" for r in biggest if r["bytes"])
    lines.extend(
        ["", "## 完整清单", "", "| 绝对路径 | 逻辑 GiB | 自动拒绝原因 |", "| --- | --- | --- |"]
    )
    for row in rows:
        reason = row["reason"].replace("|", "\\|").replace("\n", " ")
        if "WinError 5" in reason:
            reason = "访问被拒绝，未修改权限；完整内容及归档状态需人工核对"
        size = "未测" if row["bytes"] is None else f"{row['bytes'] / 1024**3:.3f}"
        lines.append(f"| `{row['path']}` | {size} | {reason} |")
    if retained:
        lines.extend(["", "## 明确保留", ""])
        lines.extend(f"- `{name}`" for name in sorted(set(retained)))
    (REPO / "docs" / "interview" / "manual-cleanup.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return {"manual_items": len(rows), "required_retained": len(set(retained))}


def prepare():
    no_link_ancestors(ARCHIVE)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (ARCHIVE / "datasets").mkdir(exist_ok=True)
    protected, rows, tracked = protected_paths()
    write(
        ARCHIVE / "protected.json",
        {
            "paths": [str(p) for p in protected],
            "c_trials": rows,
            "reason": (
                "Tracked code/docs, one demo environment, C complete evidence, "
                "frozen task sources/environments/counter and funding dependencies"
            ),
        },
    )
    verified = verify_c()
    write(ARCHIVE / "c-before.json", verified)
    if not (ARCHIVE / "space-before.json").exists():
        write(ARCHIVE / "space-before.json", free_space())
    # Full current C backup also preserves its Git checkout, not just a result summary.
    for row in rows:
        zip_archive(
            CURRENT / "trials" / row["id"],
            ARCHIVE / "datasets" / f"current-C-{row['id']}.zip",
            full=True,
        )
    zip_archive(
        CURRENT / "provider", ARCHIVE / "datasets" / "shared-funding-provider.zip", full=True
    )
    zip_archive(CURRENT, ARCHIVE / "datasets" / "current-shared-metadata.zip", shallow=True)
    planned, skipped, index = [], [], []
    roots = allowed_roots()
    todo = candidates(protected, tracked)

    def process(path):
        try:
            guard(path, roots, protected)
            before = snapshot(path)
            archive_name = hashlib.sha256(key(path).encode()).hexdigest()[:16] + ".zip"
            is_ab = path.parent == CURRENT / "trials"
            metadata_only = path.parent == REPO and path.name != "portfolio-demo-skills-2"
            m = zip_archive(
                path,
                ARCHIVE / "datasets" / archive_name,
                important=path.name in IMPORTANT,
                shallow=is_ab or metadata_only or path.parent == TEMP,
            )
            after = snapshot(path)
            if before != after:
                raise ValueError("candidate changed while archiving")
            row = {
                "path": str(path),
                "snapshot": before,
                "archive": m["archive"],
                "zip_sha256": m["zip_sha256"],
                "reason": (
                    "Archived conclusions/evidence; disposable experiment workspace "
                    "or generated test/build artifact"
                ),
            }
            entry = {
                "source": str(path),
                "archive": m["archive"],
                "retained_files": len(m["files"]),
                "coverage": m["coverage"],
            }
            return row, entry, None
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            return None, None, {"path": str(path), "reason": str(error)}

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(process, path) for path in todo]
        for number, future in enumerate(as_completed(futures), 1):
            row, entry, failure = future.result()
            if failure:
                skipped.append(failure)
            else:
                planned.append(row)
                index.append(entry)
            if number % 100 == 0:
                write(ARCHIVE / "skipped-progress.json", skipped)
                print(
                    json.dumps(
                        {
                            "prepared": number,
                            "total": len(todo),
                            "deletable": len(planned),
                            "skipped": len(skipped),
                            "sample_skips": skipped[:2],
                        }
                    ),
                    flush=True,
                )
    planned.sort(key=lambda row: key(row["path"]))
    index.sort(key=lambda row: key(row["source"]))
    write(ARCHIVE / "archive-index.json", index)
    write(
        ARCHIVE / "cleanup-plan.json",
        {
            "repository": str(REPO),
            "archive_root": str(ARCHIVE),
            "prepared_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO)
            .decode()
            .strip(),
            "allowed_roots": [str(p) for p in roots],
            "protected": [str(p) for p in protected],
            "deletions": planned,
            "skipped": skipped,
        },
    )
    print(
        json.dumps(
            {
                "plan_ready": True,
                "c": verified,
                "candidates": len(planned),
                "skipped": len(skipped),
                "logical_gib": sum(r["snapshot"]["bytes"] for r in planned) / 1024**3,
            }
        ),
        flush=True,
    )


def remove_owned_tree(path, expected_snapshot=None):
    """Native file operations only, after validation; no shell interpolation or link traversal."""
    path = Path(path)
    info = path.lstat()
    if linked(info):
        raise ValueError("reparse deletion forbidden")
    current = snapshot(path)
    if expected_snapshot is not None and current != expected_snapshot:
        raise ValueError("artifact changed immediately before deletion")
    if stat.S_ISDIR(info.st_mode):

        def readonly_file(function, filename, error):
            child = Path(filename)
            child_info = child.lstat()
            if (
                function is not os.unlink
                or not isinstance(error, PermissionError)
                or linked(child_info)
                or not within(child, path)
                or not getattr(child_info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
            ):
                raise error
            os.chmod(child, child_info.st_mode | stat.S_IWRITE)
            function(filename)

        # Both versions reject links during snapshot; callback signatures differ.
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=readonly_file)
        else:
            shutil.rmtree(
                path,
                onerror=lambda function, filename, error: readonly_file(
                    function, filename, error[1]
                ),
            )
    else:
        try:
            os.unlink(path)
        except PermissionError:
            if not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY):
                raise
            os.chmod(path, info.st_mode | stat.S_IWRITE)
            os.unlink(path)


def retry_skipped():
    """Create a fresh proposal for skipped paths; never reuse their stale fingerprints."""
    plan = read(ARCHIVE / "cleanup-plan.json")
    protected, _, _ = protected_paths()
    roots = allowed_roots()
    remaining = []
    index = read(ARCHIVE / "archive-index.json")
    for number, item in enumerate(plan["skipped"], 1):
        path = Path(item["path"])
        try:
            guard(path, roots, protected)
            before = snapshot(path)
            output = (
                ARCHIVE
                / "datasets"
                / (hashlib.sha256(key(path).encode()).hexdigest()[:16] + ".zip")
            )
            manifest = zip_archive(
                path,
                output,
                important=path.name in IMPORTANT,
                shallow=path.parent == CURRENT / "trials"
                or path.parent == REPO
                or path.parent == TEMP,
            )
            if snapshot(path) != before:
                raise ValueError("candidate changed during fresh proposal")
            plan["deletions"].append(
                {
                    "path": str(path),
                    "snapshot": before,
                    "archive": manifest["archive"],
                    "zip_sha256": manifest["zip_sha256"],
                    "reason": "Fresh stable snapshot with matching archived evidence",
                }
            )
            index.append(
                {
                    "source": str(path),
                    "archive": manifest["archive"],
                    "retained_files": len(manifest["files"]),
                    "coverage": manifest["coverage"],
                }
            )
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            remaining.append({"path": str(path), "reason": str(error)})
        if number <= 5:
            print(
                json.dumps(
                    {"retried": number, "recent_failure": remaining[-1:]}, ensure_ascii=False
                ),
                flush=True,
            )
        if number % 100 == 0:
            write(ARCHIVE / "retry-skipped-progress.json", remaining)
            print(json.dumps({"retried": number, "remaining": len(remaining)}), flush=True)
    plan["skipped"] = remaining
    write(ARCHIVE / "archive-index.json", index)
    write(ARCHIVE / "cleanup-plan.json", plan)
    print(
        json.dumps(
            {
                "candidates": len(plan["deletions"]),
                "skipped": remaining,
                "logical_gib": sum(r["snapshot"]["bytes"] for r in plan["deletions"]) / 1024**3,
            }
        ),
        flush=True,
    )


def trim_current_ab():
    plan = read(ARCHIVE / "cleanup-plan.json")
    index = read(ARCHIVE / "archive-index.json")
    trimmed = []
    protected, _, _ = protected_paths()
    for trial in csv.DictReader((CURRENT / "per-run.csv").open(encoding="utf-8")):
        if trial["arm"] not in {"A", "B"}:
            continue
        source = CURRENT / "trials" / trial["id"]
        if source.parent != CURRENT / "trials":
            raise ValueError("A/B trial outside direct trial scope")
        guard(source, allowed_roots(), protected)
        record_path = source / "record.json"
        before = snapshot(record_path)
        record = read(record_path)
        if record["arm"] != trial["arm"] or record["id"] != trial["id"]:
            raise ValueError("A/B result identity mismatch")
        unsigned = dict(record)
        if (
            digest({k: v for k, v in unsigned.items() if k != "record_sha256"})
            != record["record_sha256"]
        ):
            raise ValueError("A/B result digest mismatch")
        expected_name = hashlib.sha256(key(source).encode()).hexdigest()[:16] + ".zip"
        old = ARCHIVE / "datasets" / expected_name
        original = read(old.with_suffix(".manifest.json"))
        if original["source_root"] != str(source):
            raise ValueError("A/B archive association corrupted")
        archived_record = next(r for r in original["files"] if r["member"] == "record.json")
        if archived_record["sha256"] != sha(record_path):
            raise ValueError("A/B result changed since initial archive")
        verify_zip(original)
        output = old.with_name(old.stem + "-summary.zip")
        retained = zip_archive(source, output, shallow=True, summary_only=True)
        if snapshot(record_path) != before:
            raise ValueError("A/B result changed during summary archival")
        for row in plan["deletions"]:
            if row["path"] == str(source):
                row.update(archive=retained["archive"], zip_sha256=retained["zip_sha256"])
        for entry in index:
            if entry["source"] == str(source):
                entry.update(
                    archive=retained["archive"],
                    retained_files=len(retained["files"]),
                    coverage="Current A/B result summary only; trajectories/checkpoints discarded",
                )
        trimmed.append(
            {
                "source": str(source),
                "removed_archive": str(old),
                "removed_sha256": original["zip_sha256"],
                "summary_archive": str(output),
            }
        )
    # Save the replacement association before discarding this run's redundant A/B archives.
    write(ARCHIVE / "cleanup-plan.json", plan)
    write(ARCHIVE / "archive-index.json", index)
    write(ARCHIVE / "ab-trimming.json", trimmed)
    for item in trimmed:
        old = Path(item["removed_archive"])
        if old.parent != ARCHIVE / "datasets" or sha(old) != item["removed_sha256"]:
            raise ValueError("A/B archive trimming scope/digest mismatch")
        no_link_ancestors(old)
        no_link_ancestors(old.with_suffix(".manifest.json"))
        old.unlink()
        old.with_suffix(".manifest.json").unlink()
    print(json.dumps({"current_ab_summarized": len(trimmed)}), flush=True)


def purge(remote_commit):
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO
    ).strip():
        raise ValueError("tracked changes must be committed and uploaded before cleanup")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=REPO).decode().strip()
    remote = (
        subprocess.check_output(
            [
                "git",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "ls-remote",
                "origin",
                f"refs/heads/{branch}",
            ],
            cwd=REPO,
        )
        .decode()
        .split()
    )
    if head != remote_commit or not remote or remote[0] != head:
        raise ValueError("GitHub exact backup gate failed")
    plan = read(ARCHIVE / "cleanup-plan.json")
    if plan["repository"] != str(REPO) or plan["archive_root"] != str(ARCHIVE):
        raise ValueError("cleanup plan identity mismatch")
    current_protected, _, _ = protected_paths()
    protected = [Path(p) for p in plan["protected"]] + current_protected
    roots = allowed_roots()
    verified = verify_c()
    if verified != read(ARCHIVE / "c-before.json"):
        raise ValueError("C evidence changed since prepare")
    write(ARCHIVE / "github-backup.json", {"commit": head, "branch": branch, "remote": remote})
    bundle = ARCHIVE / "tracefix-code.bundle"
    heads = (
        subprocess.check_output(["git", "bundle", "list-heads", str(bundle)], cwd=REPO).decode()
        if bundle.exists()
        else ""
    )
    if head not in heads:
        temporary_bundle = bundle.with_suffix(".bundle.writing")
        subprocess.run(
            ["git", "bundle", "create", str(temporary_bundle), "--all"], cwd=REPO, check=True
        )
        os.replace(temporary_bundle, bundle)
    subprocess.run(
        ["git", "bundle", "verify", str(bundle)], cwd=REPO, check=True, stdout=subprocess.DEVNULL
    )
    write(
        ARCHIVE / "code-backup.json", {"commit": head, "bundle": str(bundle), "sha256": sha(bundle)}
    )
    journal = ARCHIVE / "deletion-journal.jsonl"
    done = (
        {
            r["path"]
            for r in (json.loads(s) for s in journal.read_text(encoding="utf-8").splitlines())
            if r.get("status") == "deleted"
        }
        if journal.exists()
        else set()
    )
    for number, row in enumerate(plan["deletions"], 1):
        path = Path(row["path"])
        if str(path) in done:
            continue
        result = {"path": str(path), "bytes": row["snapshot"]["bytes"], "time": time.time()}
        try:
            guard(path, roots, protected)
            if snapshot(path) != row["snapshot"]:
                raise ValueError("artifact changed after prepare")
            manifest = read(Path(row["archive"]).with_suffix(".manifest.json"))
            if manifest["source_root"] != str(path) or manifest["zip_sha256"] != row["zip_sha256"]:
                raise ValueError("candidate archive association corrupted")
            verify_zip(manifest)
            remove_owned_tree(path, row["snapshot"])
            result["status"] = "deleted"
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            result.update(status="skipped", reason=str(error))
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if number % 20 == 0:
            print(json.dumps({"processed": number, "total": len(plan["deletions"])}), flush=True)
    after = verify_c()
    if after != verified:
        raise ValueError("C post-cleanup verification failed")
    results = [json.loads(s) for s in journal.read_text(encoding="utf-8").splitlines()]
    report = {
        "github": head,
        "c_verified": after,
        "space_before": read(ARCHIVE / "space-before.json"),
        "space_after": free_space(),
        "deleted_items": sum(r["status"] == "deleted" for r in results),
        "deleted_logical_bytes": sum(r["bytes"] for r in results if r["status"] == "deleted"),
        "skipped": plan["skipped"] + [r for r in results if r["status"] == "skipped"],
        "scope": "Only explicit TraceFix artifact roots; shared Docker/WSL/Codex parents untouched",
    }
    write(ARCHIVE / "final-report.json", report)
    report["archive_catalog"] = archive_catalog()
    write(ARCHIVE / "final-report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("prepare", "retry", "trim-ab", "manual-list", "verify", "purge")
    )
    parser.add_argument("--github-commit")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    elif args.mode == "retry":
        retry_skipped()
    elif args.mode == "trim-ab":
        trim_current_ab()
    elif args.mode == "manual-list":
        print(json.dumps(manual_cleanup_report()))
    elif args.mode == "verify":
        print(json.dumps(verify_c()))
    elif args.github_commit:
        purge(args.github_commit)
    else:
        parser.error("purge requires --github-commit")


if __name__ == "__main__":
    main()
