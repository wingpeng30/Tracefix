# Fresh authorized three-task campaign

2026-10-02 user authorized maximum five logical Agent runs/CNY five total;
failures count, either bound stops. Old eight-run evidence is untouched.
First runs: more-itertools, Markdown, Click. Two reserved rechecks require a
reproduced issue and recorded fix, not sampling until success.

Entrypoint: `python scripts/live_public_tasks.py --root runs/live-public-three-types-20261002 --task <id> --qualification <qualified-record.json> --env-file .env`.
`--prepare-only` is zero-call and writes separate evidence. Qualified source,
manifest and environment identities, process lock, fresh durable run/request
ledgers, recorded regression targets and independent verification are required.
Unknown request/result/usage stops further paid calls. Reference patches remain
outside the Agent checkout.

Official DeepSeek Flash, non-thinking, non-streaming, zero retries, local backend,
Skills/MCP off. Per run: 20 steps, 60,000 input/8,000 output cumulative tokens,
2,048 output per request, 16 tests, 900 active seconds, 60-second timeout.
Qualified request counting is unchanged; unavailable hard counter must stop.
Official 2026-10-02 peak prices: CNY 2/M uncached input, CNY 8/M output;
snapshot in `runs/live-public-three-types-20261002/prices.json`. Client reservation
is not a supplier billing hard cap. Request views remain local and reviewed.

Offline preparation: six ledger/budget tests passed with explicit Python 3.12;
Ruff, compileall and Diff passed. Default shell Python was unrelated Anaconda.
Sandbox temporary ACL blocked pytest; a new unsandboxed temporary directory
passed. Failed environment attempts are not behavioral evidence.

Run results will be appended after execution; no live success claimed yet.

First three runs at `4201ab5`: more-itertools completed and independently passed;
Markdown stopped after 65,671 input tokens with a failed independent patch;
Click stopped after 72,869 input tokens without a patch. These demonstrate the
ordinary cumulative Token limit was post-response, not a supplier hard cap.
All 23 requests have valid usage and no pending result. Source remained unchanged.

Targeted correction: new campaign adapter checks conservative per-request input
reservation plus known cumulative usage before sending; output is also reserved.
Formal P2 counting guards are unchanged. Rechecks lower the existing deterministic
compaction trigger from 32,000 to 6,000 estimated tokens; budgets are unchanged.
Sixteen offline tests (ledger, request rejection, context) passed. Rechecks are
two newly counted logical runs, not resumes or replacements for failed results.

## Actual outcome and stop (2026-10-02)

Executed **4/5 logical runs**, then stopped conservatively. 27 completed provider
requests, zero pending entries. Peak-price/all-cache-miss conservative accounting
is **CNY 0.466682**, not actual supplier debit. No fifth run or repeated sampling.

| Run | Code | Run ID | Outcome |
| --- | --- | --- | --- |
| more-itertools | `4201ab5` | `20261002T075529Z-fa6b5267` | Completed; gate and independent verification pass; 36 task + 6 regression cases; patch two added lines |
| Markdown | `4201ab5` | `20261002T075702Z-004cebe3` | Input budget stop at 65,671; patch fails independent verification; not completed |
| Click | `4201ab5` | `20261002T075848Z-65ff3364` | Input budget stop at 72,869; no patch; not completed |
| Markdown targeted recheck | `cb3923d` | `20261002T080320Z-28a72290` | Local pre-request input reservation refusal after four completed requests, 27,198 actual input; no patch; not completed |

All prepared source repositories remain unchanged. Raw runs are in
`runs/live-public-three-types-20261002/runs/<ID>/`; each includes result,
trajectory, original tool/test evidence and checkpoint. Stage directories hold
config, report, export when nonempty and artifact hashes. Independent verification
is separate from Agent termination: it does not turn a budget stop into success.

Request ledger SHA-256:
`fca3a7eb8faf5e0a6e3ff9bfc2d43dd0cd55d709cf8ab553a3a94e001bbb2605`.
Run ledger SHA-256:
`0e00c0aeb2263a2057197ae6aa0effb2861272f6de99f447fdf01e203438f3ab`.
Successful patch SHA-256:
`dc2374cc8a5d066f40acd79b87cdd2fe5832d9a326a28d6ab861fec06522c684`.
Failed Markdown patch SHA-256:
`ee8a28515b8de10ba98bbad6cd172d9862e2b8ce8cb382ab57b614dde5ff81c5`.

The fourth run revealed that generic adapter exceptions mark usage/cost unknown
even for a guaranteed pre-transmission local refusal. Its original result remains
unchanged (`usage_complete=false`); this blocked the fifth paid run by policy.
The request ledger independently shows no unknown provider request. A new
`PreRequestBudgetExceeded` contract now preserves prior known/unknown accounting
only when an adapter guarantees it never invoked transport; ordinary provider
errors still mark consumption unknown. 55 focused offline tests passed, including
known refusal, previous unknown state, and genuine provider-error negative cases.
No paid call was made after this correction.

Next highest priority: reduce repeated broad source reads and make budget-aware
context selection effective before the model consumes most input allowance.
Click read seven ranges without reaching a patch; lowering compaction alone did
not establish success. Use these recorded histories offline to test targeted
retrieval, retained source evidence and earlier useful compaction before proposing
another live campaign. Do not increase budgets or weaken task assertions merely
to obtain successful samples. Four small runs do not estimate overall success.
