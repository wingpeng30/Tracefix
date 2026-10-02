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

The first frozen Markdown candidate `ff9a046a6488e84513d053ce4c12f34ce5736c99`
qualified locally with no tracked changes:
`runs/qualify markdown frozen ff9a046/qualification.json`, SHA-256
`8c8358e1db35acc933d21075e69fda28beeb212eceefafe08dcd335a8f7130a2`.
The upstream audit has one failed task assertion; corrected references have 9/9,
2/2 and 19/19 passing tests. Replay uses five pytest processes and independent
verification is separately recorded. This candidate is not the final review head.

A read-only review found additional identity/scope gates to strengthen: reject
dirty tracked implementations and identity changes during execution; validate
new tasks' actual single parent; reject undeclared package files and product
patches that modify anything outside declared product files. Fifteen targeted
behavior tests pass after these fixes. New exact-head CI and local qualification
are required; prior green checks do not approve the modified head.

CI #83 (`ff9a046`, merge checkout `b31a40045c3acae3989cbf0d66f428cdd008ea5c`)
passed engineering checks and Linux jobs but both wheel qualifications failed on
the frozen existing Markdown tests. The clean wheel environment had no Markdown
extension entry-point metadata; the local interpreter happened to have Markdown
3.7. The official PyPI 3.5.2 universal wheel is now explicitly locked with SHA-256
`d43323865d89fc0cb9b20c75fc8ad313af307cc087e84b657d9eec768eddeadd`.
Preparation is explicit, never an automatic qualifier install. A new preflight
requires `attr_list`, `fenced_code`, `tables` metadata and the production audit
still checks checkout module origins. The qualifier also now rejects non-assertion
exceptions in expected failed tests using original JUnit, preventing an import
exception inside a test body from masquerading as a reproduced defect.

## Markdown delivered; Click candidate

PR #12 is merged as `6b5af671c9154f867612675e80307dfa4eb2ac42`. Exact head
`24dbaa911503e6cd904d452833639bc2c05641d9` passed [CI #86](https://github.com/wingpeng30/Tracefix/actions/runs/36975942940):
all five jobs, Windows 3.11/3.12 each 819 tests with zero failures/errors/skips,
pytest-only coverage `13621/15126 = 90.05024461192649%`, Ruff, compileall, Diff,
editable and outside-repository wheel, original task and Markdown qualification,
Linux frozen/ordinary Docker and Serena MCP. CI #83 and #84 failures remain;
CI #85 passed an earlier head, so #86 is the merge gate.

Local exact-head Markdown outside-wheel evidence:
`runs/qualify markdown outside wheel 24dbaa9/qualification.json`, SHA-256
`64ebdc8b2bbc30b971d2f02ad7cf06126c0cdda5499de05cc4ad32996a095a46`.
The installed wheel SHA-256 is
`666d0f675bcf208353b740d53927584094e62fd07814f1d0cdd56deaf71bb87b`
(built at `20647d1`; runtime/package files unchanged by the final script-only fix).
Both installed environment and working directory were outside this repository.
Original task's default CLI also qualified at `24dbaa9`:
`runs/qualify default task 24dbaa9/qualification.json`, SHA-256
`7321b30c89b05d2f6d0d6da58b4737dd1570acb75df9a09ed826f26e4e7ee2f1`.

Click's temporary prequalification passed on 3.12 at
`runs/qualify click preliminary 02`; this used an explicit temporary package map
and is exploration, not final delivery evidence. Four task cases plus three
existing single-node regression targets run per checkout. The original upstream
patch passes the selected existing tests but fails two new cases; the separately
approved two-line correction passes all seven. A diagnostic mutant dropping
single-dash completions passes the four task cases but fails two existing targets.
The production replay consumes seven Agent pytest processes; independent
verification separately runs seven more. Final Click CI and commit follow in PR #13.

## Final delivery (2026-10-02)

PR #13 merged as `47f2619f618dbaef658e3071309d34a5e10bc8a2` after exact
head `351b600c2680633f02ec4c4cd32707e1e2aa5253` passed
[CI #88](https://github.com/wingpeng30/Tracefix/actions/runs/36977521723).
Both Windows versions ran 820 tests, zero failures/errors/skips, with pytest-only
coverage `13621/15126 = 90.05024461192649%`. All five jobs passed, including
outside-repository installed-wheel qualification of Markdown and Click, the
original default task, engineering gates and existing Linux Docker/MCP checks.
Review found no blocker and GitHub had no unresolved review threads. The merge
tree matches the approved head; historical untracked materials were preserved.

Formal local Click qualification (supersedes the temporary-map exploration):
`runs/qualify click outside wheel 351b600/qualification.json`, SHA-256
`1cd891e2b845bfc14942fecb663b60cc810ae4cdb5273b9ae24146d24fb5ce9f`.
The source clone remained unchanged. Both repeated base/corrected-reference
matrices, raw upstream failure audit and diagnostic mutant matched their declared
outcomes; production reference replay and independent verification passed.
Raw outputs, JUnit, identities and hashes remain in the local record and CI
artifacts. PR #13's final description also indexes this evidence.

All three defect types are qualified within the frozen contracts. Markdown and
Click use upstream product patches plus separately approved corrective patches;
their original upstream commits alone do not satisfy these task contracts.
Reference replay is not autonomous model repair, and qualification establishes
neither overall repair success rate nor comparative component effectiveness.

## Proposed fresh real-model campaign — not authorized or executed

- Freeze code at the approved merge `47f2619` and the three packaged manifests;
  record full code, prepared task, config and patch identities before each run.
- Run each task once, serially, with at most two additional targeted rechecks
  after a reproduced failure is fixed: **at most five logical Agent runs and
  CNY 5 total**, stopping at either bound. Failed runs count; resume retains the
  same logical identity and cumulative budgets. The old eight-run allowance is
  exhausted and is not reused.
- Proposed model: `deepseek/deepseek-flash`, non-thinking, non-streaming,
  automatic retries disabled; local backend, Skills/MCP disabled initially.
  Per run: 20 steps, cumulative input 60,000/output 8,000 tokens, 2,048 output
  per request, 16 pytest processes, 900 active seconds, 60-second request timeout.
- Before any paid request, prepare a new independent campaign ledger and
  regression-aware configuration. Do not reuse the old hard-coded eight-run,
  CNY 20 campaign script unchanged. Offline-test the request-before-reservation
  rejection, pending request recovery, target budgets and missing usage paths.
- Freeze execution-day official prices from
  [DeepSeek pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/).
  Reserve conservatively using peak cache-miss input and maximum output prices;
  reconcile valid usage after responses. Client estimates are not an account-side
  billing cap. Unknown usage/cost/result or persistence failure stops further
  paid requests; never resend an uncertain request automatically.
- Give the Agent task text and protected targets, but no reference/corrective
  patch. Record request views securely; independently verify the final patch in
  fresh checkouts, then report/export. Keep failures and budget stops in the
  ledger. Publish reviewed summaries rather than full private request contents.

The next action requires fresh user approval of this count and amount, followed
by the offline campaign preflight. No supplier request was made in this batch.
