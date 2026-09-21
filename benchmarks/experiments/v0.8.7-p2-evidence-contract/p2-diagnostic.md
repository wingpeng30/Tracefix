# P2 已有轨迹诊断

本报告只读取已保存工件，未发起供应商请求。

- 终止分类：`{"agent_completed": 24, "infrastructure_error": 1, "normal_trial_budget_stop": 17, "request_bound_violation": 1, "step_limit_exceeded": 12, "test_limit_exceeded": 5}`
- 测试修改分类：`{"config_modified": 1, "source_and_test": 28, "test_only": 3}`
- 机制计数：`{"compaction_32k": {"character_delta": -561378, "compaction_count": 0, "estimated_tokens_saved": 140332, "failed_tool_calls": 103, "file_rereads": 116, "max_context_tokens": 21434, "original_chars": 1580658, "presented_chars": 1019280, "repo_map_candidate_count": 360, "repo_map_candidate_reads": 52, "repo_map_present": 30, "tool_results_changed": 662, "tool_results_lengthened": 0, "tool_results_shortened": 662, "tool_results_unchanged": 0, "trials": 30}, "no_compaction": {"character_delta": 0, "compaction_count": 0, "estimated_tokens_saved": 0, "failed_tool_calls": 101, "file_rereads": 75, "max_context_tokens": 31868, "original_chars": 1443163, "presented_chars": 1443163, "repo_map_candidate_reads": 0, "repo_map_present": 0, "tool_results_changed": 0, "tool_results_lengthened": 0, "tool_results_shortened": 0, "tool_results_unchanged": 703, "trials": 30}}`
- 压缩实际触发：False。现有输入 Token 差异不能归因于上下文压缩。
- 基础设施事故：1 次。
