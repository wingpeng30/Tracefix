# P2 四组消融离线详细诊断

执行版本：`a8309eb5a20aab7825c797ee8467eacf8c42c3b0`；固定位置 120/120，
有效证据 120；供应商响应核验 2094。
分析期间付费请求和供应商客户端均为 0。

报告只保存计数、哈希、相对试次编号及轨迹事件行号。缺失值保留 null。
模型请求视图、工具输出、任务描述和补丁正文仍只在原始本机轨迹中。

逐对基线与行动优化诊断见 `paired-cases.json`。每对按任务 ID 和重复编号对齐；首次差异是可观察事件差异，不是因果归因。

## 主要集合任务等权均值

- `no_compaction`：input_tokens=198596.625; output_tokens=3085.75; agent_seconds=45.79999999999321; verification_seconds=7.542375000006966; model_request_seconds=21.502240366678354; tool_execution_seconds=18.349250000006577; context_preparation_seconds=0.02349605415899229; repository_index_seconds=0.0; model_response_event_seconds=21.502240708333332; unassigned_agent_seconds=5.925013579149284; request_cycles=17.916666666666668; tool_call_count=23.5; cached_tool_calls=0; failed_tool_calls=3.0833333333333335; repeated_file_reads=3.0416666666666665; unique_file_reads=4.041666666666667; provider_prompt_cache_hit_tokens=183125.33333333334; provider_prompt_cache_miss_tokens=15471.291666666666; request_count=17.458333333333332; test_tool_seconds_nested_in_tool_execution=9.1008750000013; context_prepared_events=17.916666666666668; context_before_peak_estimated_tokens=16734.5; context_after_peak_estimated_tokens=16734.5; context_tool_pruned_results_sum=0; history_fold_event_count=0; history_fold_tool_results_pruned_sum=0; unique_tool_result_count=23.5; repeated_request_tool_view_count=222.66666666666666; tool_presentation_changed_count=0; tool_presentation_shortened_count=0; tool_presentation_lengthened_count=0; repo_map_added_event_count=0; repo_map_in_request_count=0; repo_map_candidate_reads=0
- `repo_map_only`：input_tokens=210458.0833333333; output_tokens=3163.9166666666665; agent_seconds=46.58220833334053; verification_seconds=7.220666666677668; model_request_seconds=21.656047366663795; tool_execution_seconds=15.402333333336477; context_preparation_seconds=0.0212570125052783; repository_index_seconds=3.215458333327357; model_response_event_seconds=21.656047; unassigned_agent_seconds=6.287112287507625; request_cycles=17.25; tool_call_count=21.458333333333336; cached_tool_calls=0; failed_tool_calls=3.875; repeated_file_reads=3.25; unique_file_reads=3.7083333333333335; provider_prompt_cache_hit_tokens=195594.66666666666; provider_prompt_cache_miss_tokens=14863.416666666666; request_count=16.791666666666668; test_tool_seconds_nested_in_tool_execution=10.330124999995556; context_prepared_events=17.25; context_before_peak_estimated_tokens=16908.416666666668; context_after_peak_estimated_tokens=16908.416666666668; context_tool_pruned_results_sum=0; history_fold_event_count=0; history_fold_tool_results_pruned_sum=0; unique_tool_result_count=21.666666666666668; repeated_request_tool_view_count=200.625; tool_presentation_changed_count=0; tool_presentation_shortened_count=0; tool_presentation_lengthened_count=0; repo_map_added_event_count=1; repo_map_in_request_count=17.25; repo_map_candidate_reads=1.625
- `action_optimization_bundle`：input_tokens=127551.33333333333; output_tokens=2820.875; agent_seconds=46.02924999999717; verification_seconds=9.518250000003414; model_request_seconds=19.115752770861338; tool_execution_seconds=21.04970833335392; context_preparation_seconds=0.01589428331014157; repository_index_seconds=0.0; model_response_event_seconds=19.115753291666667; unassigned_agent_seconds=5.847894612471767; request_cycles=15.75; tool_call_count=17.666666666666668; cached_tool_calls=0; failed_tool_calls=3.958333333333333; repeated_file_reads=3.0; unique_file_reads=2.2916666666666665; provider_prompt_cache_hit_tokens=118250.66666666667; provider_prompt_cache_miss_tokens=9300.666666666666; request_count=15.75; test_tool_seconds_nested_in_tool_execution=14.020916666685176; context_prepared_events=15.75; context_before_peak_estimated_tokens=10637.5; context_after_peak_estimated_tokens=10637.5; context_tool_pruned_results_sum=0; history_fold_event_count=0; history_fold_tool_results_pruned_sum=0; unique_tool_result_count=17.708333333333332; repeated_request_tool_view_count=147.58333333333334; tool_presentation_changed_count=17.708333333333332; tool_presentation_shortened_count=17.708333333333332; tool_presentation_lengthened_count=0; repo_map_added_event_count=0; repo_map_in_request_count=0; repo_map_candidate_reads=0
- `context_management_only`：input_tokens=201691.58333333334; output_tokens=3169.75; agent_seconds=44.20912499999152; verification_seconds=8.04108333333958; model_request_seconds=22.26715069999288; tool_execution_seconds=15.582916666642026; context_preparation_seconds=0.03215082082169829; repository_index_seconds=0.0; model_response_event_seconds=22.267150416666667; unassigned_agent_seconds=6.3269068125349195; request_cycles=18.958333333333332; tool_call_count=24.25; cached_tool_calls=0; failed_tool_calls=3.7916666666666665; repeated_file_reads=3.875; unique_file_reads=4.041666666666667; provider_prompt_cache_hit_tokens=188021.33333333334; provider_prompt_cache_miss_tokens=13670.25; request_count=18.541666666666664; test_tool_seconds_nested_in_tool_execution=8.24279166667111; context_prepared_events=18.958333333333332; context_before_peak_estimated_tokens=17027.0; context_after_peak_estimated_tokens=15193.375; context_tool_pruned_results_sum=9.583333333333334; history_fold_event_count=7.958333333333333; history_fold_tool_results_pruned_sum=9.583333333333334; unique_tool_result_count=24.416666666666668; repeated_request_tool_view_count=242.375; tool_presentation_changed_count=0; tool_presentation_shortened_count=0; tool_presentation_lengthened_count=0; repo_map_added_event_count=0; repo_map_in_request_count=0; repo_map_candidate_reads=0

## 基线与行动优化配对

普通集合 24 对：5 对退步、3 对反向、16 对结果相同；另有 6 对特殊资格单列。

| 任务 | 重复 | 方向 | 基线/行动试次 | 首个差异 | 验收基线/行动 | 输入差 | Agent 秒差 | 标记变化 |
| --- | ---: | --- | --- | --- | --- | ---: | ---: | --- |
| pytest-dev__pytest-10051 | 1 | regression | 24/22 | tool_results @13/13 | passed/pytest execution failed | -159499 | -19.657 | {"missing_repository_path": {"action": 0, "baseline": 1}, "tool_results": {"action": 12, "baseline": 27}, "tool_results_shortened": {"action": 12, "baseline": 0}} |
| pytest-dev__pytest-10081 | 1 | regression | 27/25 | tool_results @13/13 | passed/empty_agent_patch | -51380 | 1.579 | {"missing_assertion_keyword": {"action": 2, "baseline": 0}, "missing_exception_class": {"action": 1, "baseline": 0}, "missing_repository_path": {"action": 10, "baseline": 0}, "tool_results": {"action": 26, "baseline": 29}, "tool_results_shortened": {"action": 26, "baseline": 0}} |
| pytest-dev__pytest-10356 | 3 | regression | 112/110 | tool_results @13/13 | passed/pytest execution failed | -73586 | 3.188 | {"missing_repository_path": {"action": 6, "baseline": 0}, "tool_results": {"action": 26, "baseline": 28}, "tool_results_shortened": {"action": 26, "baseline": 0}} |
| sphinx-doc__sphinx-10435 | 1 | regression | 33/35 | assistant_decision @9/9 | passed/pytest execution failed | -117909 | 7.687 | {"missing_assertion_keyword": {"action": 2, "baseline": 0}, "missing_repository_path": {"action": 6, "baseline": 0}, "tool_results": {"action": 19, "baseline": 27}, "tool_results_shortened": {"action": 19, "baseline": 0}} |
| sphinx-doc__sphinx-10435 | 2 | regression | 76/74 | assistant_decision @9/9 | passed/pytest execution failed | -19776 | -0.735 | {"missing_assertion_keyword": {"action": 2, "baseline": 0}, "missing_repository_path": {"action": 7, "baseline": 0}, "missing_test_node": {"action": 1, "baseline": 0}, "tool_results_shortened": {"action": 26, "baseline": 0}} |
| sphinx-doc__sphinx-10435 | 3 | reverse | 115/113 | tool_results @13/13 | pytest execution failed/passed | -18860 | 52.875 | {"missing_assertion_keyword": {"action": 4, "baseline": 0}, "missing_exception_class": {"action": 1, "baseline": 0}, "missing_repository_path": {"action": 11, "baseline": 0}, "missing_test_node": {"action": 2, "baseline": 0}, "tool_results": {"action": 26, "baseline": 28}, "tool_results_shortened": {"action": 26, "baseline": 0}} |
| sphinx-doc__sphinx-10449 | 2 | reverse | 79/77 | assistant_decision @9/9 | empty_agent_patch/passed | -105327 | 7.813 | {"missing_exception_class": {"action": 1, "baseline": 0}, "missing_repository_path": {"action": 1, "baseline": 0}, "tool_results": {"action": 18, "baseline": 22}, "tool_results_shortened": {"action": 18, "baseline": 0}} |
| sphinx-doc__sphinx-10449 | 3 | reverse | 118/120 | assistant_decision @9/9 | empty_agent_patch/passed | -166193 | -27.360 | {"tool_results": {"action": 15, "baseline": 28}, "tool_results_shortened": {"action": 15, "baseline": 0}} |

## 耗时分解：普通题内重复均值后任务等权

| 组别 | Agent 秒 | 验收秒 | 模型调用边界秒 | 工具秒 | 测试工具嵌套秒 | 上下文准备秒 | Repo Map 构建秒 | 未归属秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no_compaction | 45.800 | 7.542 | 21.502 | 18.349 | 9.101 | 0.023 | 0.000 | 5.925 |
| repo_map_only | 46.582 | 7.221 | 21.656 | 15.402 | 10.330 | 0.021 | 3.215 | 6.287 |
| action_optimization_bundle | 46.029 | 9.518 | 19.116 | 21.050 | 14.021 | 0.016 | 0.000 | 5.848 |
| context_management_only | 44.209 | 8.041 | 22.267 | 15.583 | 8.243 | 0.032 | 0.000 | 6.327 |

模型调用边界含客户端和账本处理，不是纯供应商响应时间；响应事件时间是边界内部观测，不相加。测试工具耗时属于工具总耗时的子项。

## 机制指标：每组 30 项

| 组别 | 请求 | Repo Map 入请求 | 候选读取 | 重复工具视图 | 缓存命中调用 | 失败工具调用 | 输出缩短/变长 | 上下文裁剪 | 历史折叠 | 最大裁剪前/后估算峰值 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| no_compaction | 525 | 0 | 0 | 6930 | 0 | 106 | 0/0 | 0 | 0 | 30885/30885 |
| repo_map_only | 502 | 519 | 55 | 6113 | 0 | 108 | 0/0 | 0 | 0 | 26069/26069 |
| action_optimization_bundle | 519 | 0 | 0 | 5612 | 0 | 104 | 603/0 | 0 | 0 | 20979/20979 |
| context_management_only | 548 | 0 | 0 | 7220 | 0 | 112 | 0/0 | 417 | 253 | 33649/27124 |

行动优化缩短 603 条工具输出；原始字符 1405221，呈现字符 919967。字符差不是供应商 Token 节省。上下文裁剪与历史折叠独立计数。
上下文组最大本地估算峰值为 33649→27124。

## 失败分布与改进决策

原验收分类不变：48 通过、24 空补丁、35 pytest 执行失败、12 收集失败、1 测试/配置修改拒绝。12 条 skipped run_tests 结果和 50 个预算尾部周期均保留并标注预算原因。
尚不能证明优化实现存在可复现缺陷，本轮不修改优化策略。事后启发式标记缺失不能证明语义必要性。
单变量假设：工具输出呈现缩短可能降低输入，同时遮蔽部分诊断特征。下一开发集仅启用呈现，关闭行动引导和读取缓存；固定模型、题目、提示、评分、预算和重复数，预先冻结成功、用量、标记保留和资源终止分析。先拆分呈现开关并验证合成回归；20 题留出集保持未使用。

## 限制

- This is post-hoc offline evidence description, not a randomized new experiment or causal estimate.
- Elapsed model-call boundary includes client processing and ledger settlement; it is not provider-only latency.
- Test-tool durations are a subset of total tool-execution time and must not be added again.
- Repeated tool-result views can count the same content repeatedly; character counts and local token estimates are not provider token savings.
- Missing cache or timing fields remain null; no missing evidence is converted to zero.
- Marker categories identify only configured signatures, not semantic sufficiency or model intent.
