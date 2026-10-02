# Three public task types: execution record

Starting main: `f2e735144cfaafab3b1d2e73425e90304ba35345`.
This batch adds parsing/cooperation and resource-lifecycle tasks to the already
qualified iterator empty-input task. All model responses are recorded replay;
supplier requests, holdout access and new paid experiments are prohibited.

## Reference audit and authorized change

Both actual reference parents were confirmed locally from Git objects:

| Task | Base | Upstream reference |
| --- | --- | --- |
| Markdown #1414 | `9edba85fc14f034b7109534220702bf60178ff15` | `3d8afc6f89e169522f44c1bbec15f66dc359eccb` |
| Click #2800 | `d8763b93021c416549b5f8b4b5497234619410db` | `0ef55fda47bb3d0dece21197d81d8d72a4250e73` |

Markdown's original product fix moved inline `} ` behind trailing text. The raw
failed qualification is at `runs/qualify markdown initial/qualification.json`.
One initial invalid-fence diagnostic mistakenly rejected preserved literal text;
that assertion was corrected to inspect rendered code attributes, while the tail
preservation assertion remains unchanged. The first correction still displaced
whitespace (`runs/qualify markdown corrected 01`); the next reference passed all
task tests but its diagnostic mutant was too broad (`corrected 02`). These are
development/qualification failures, not hidden or rescored model runs.

Click's original fix returns `leaf` instead of `red`, `blue` for nested option
completion. A read-only secondary audit identified wrong parent argument lists;
the local independent source probe confirmed the failure. No supplier called.

The user explicitly approved retaining both tasks with separately hashed minimal
correction patches. Upstream product patches and identities remain unchanged.
Qualification must distinguish corrected references from upstream commits.

## Delivery and validation

The qualification selector accepts reviewed task IDs; the original command still
defaults to more-itertools. Schema 2 adds multiple product identities and regression
targets. Production setup/tools/adapter/gate/verify/report/export remain in use.
Windows dual-version and outside-checkout wheel qualification is part of CI.
Actual final commits, CI and qualification records are recorded below when available.

Until all three task types pass, no new real-model experiment is eligible.
