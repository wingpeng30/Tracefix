import json

from tracefix.real_candidates import CandidateCollectionConfig, collect_candidates, patch_paths


def _patch(source: str, test: str) -> str:
    return (
        f"diff --git a/{source} b/{source}\n--- a/{source}\n+++ b/{source}\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        f"diff --git a/{test} b/{test}\n--- a/{test}\n+++ b/{test}\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )


def test_patch_paths_normalize_and_deduplicate() -> None:
    value = _patch("pkg/a.py", "tests/test_a.py") + "diff --git a/pkg/a.py b/pkg/a.py\n"
    assert patch_paths(value) == ("pkg/a.py", "tests/test_a.py")


def test_collect_candidates_reads_local_fixture_and_enforces_repository_quota(tmp_path) -> None:
    rows = []
    repos = (
        "pytest-dev/pytest",
        "pylint-dev/pylint",
        "sphinx-doc/sphinx",
        "psf/requests",
    )
    for repo in repos:
        owner, name = repo.split("/")
        for number in range(3):
            instance_id = f"{owner}__{name}-{number}"
            rows.append(
                {
                    "instance_id": instance_id,
                    "repo": repo,
                    "base_commit": "a" * 40,
                    "version": "1.0",
                    "problem_statement": f"fix {instance_id}",
                    "patch": _patch("pkg/one.py", "pkg/two.py"),
                    "test_patch": _patch("tests/test_one.py", "tests/test_two.py"),
                }
            )
    source = tmp_path / "rows.json"
    source.write_text(json.dumps(rows), encoding="utf-8")
    result = collect_candidates(
        CandidateCollectionConfig(source=source, output_dir=tmp_path / "out")
    )
    assert len(result.selected) == 12
    assert {item.repo for item in result.selected} == set(repos)
    assert all(
        (tmp_path / "out" / item.instance_id / "gold.patch").is_file() for item in result.selected
    )
