# Shell completion context resource lifecycle

Repair Click #2644 / #2800. Contexts created during shell completion must release registered context-manager resources and call close callbacks exactly once before returning. Cover ordinary command, group, nested group and chain. Preserve Choice value completion, child argument consumption, existing command/short-option and chain completion behavior. Do not modify tests. No real shell or external service is required; queries use ShellComplete directly. This contract covers the listed targets, not all Click functionality.

Required task target: `tests/test_tracefix_completion_resources.py`.
Required regressions: `tests/test_shell_completion.py::test_command`, `tests/test_shell_completion.py::test_chained`, `tests/test_shell_completion.py::test_group_command_same_option`.
The source is under `src/click`; imports must resolve to the task checkout. Source: https://github.com/pallets/click/pull/2800 . Upstream product fix plus separate reviewed child-argument correction is the reference; no golden patch is placed in Agent checkout.
