# RC1.3.3-Lite-P1-HF4.1-R1.1 最终构建报告

构建时间：2026-07-27T06:49:43.532007+00:00

当前状态：`RC1.3.3-Lite-P1-HF4.1-R1.1 零来源正式运行主链热修完成，Setup已构建并安装烟测，等待用户最终文章复测。`

## 结论

- Setup 与卸载统一使用 Inno Setup 的 `unins000.exe`。
- 安装目录统一为 `%LOCALAPPDATA%\Programs\热点图文批量生产工作台`。
- 单热点生成多篇的批次并发元数据已统一限制为 3。
- 安装卸载检查失败时不会继续交付。

## 安装卸载检查

```json
{
  "INNO_SETUP_INSTALL_PASS": true,
  "WINDOWS_APPS_ENTRY_PASS": true,
  "INNO_UNINSTALL_REAL_PASS": true,
  "INSTALL_DIR_REMOVED_PASS": true,
  "USER_DATA_PRESERVED_PASS": true,
  "install_returncode": 0,
  "uninstall_returncode": 0,
  "install_dir": "C:\\Users\\lenovo\\AppData\\Local\\Programs\\热点图文批量生产工作台",
  "data_dir": "C:\\Users\\lenovo\\AppData\\Local\\热点图文批量生产工作台"
}
```

## 测试记录

```json
{
  "release": "RC1.3.3-Lite-P1-HF4.1-R1.1",
  "created_at": "2026-07-27T06:45:41.153093+00:00",
  "py_compile": {
    "status": "passed",
    "exit_code": 0,
    "command": "python -m compileall -q generation export license_admin modules scripts tests"
  },
  "pytest": {
    "status": "passed",
    "exit_code": 0,
    "passed": 30,
    "failed": 0,
    "command": "python -m pytest tests\\test_p1_hf3_json_fallback.py tests\\test_p1_hf3_speed.py tests\\test_p1_hf4_content_layout_speed.py tests\\test_p1_hf4_1_closure.py tests\\test_p1_hf4_1_r1.py tests\\test_p1_hf4_1_r1_1_runtime.py -q --basetemp .pytest_tmp_hf4_1_r1_1_full",
    "summary": "30 passed in 6.15s"
  },
  "r1_1_gates": {
    "RUNTIME_ZERO_SOURCE_LIMITED_DRAFT_PASS": true,
    "RUNTIME_ZERO_SOURCE_TEXT_MODEL_CALL_PASS": true,
    "RUNTIME_ZERO_SOURCE_WORD_EXPORT_PASS": true,
    "RUNTIME_LIMITED_QUALITY_WARNING_PASS": true,
    "RUNTIME_EMPTY_TOPIC_STILL_FAILS_PASS": true,
    "RUNTIME_REAL_SOURCE_MODE_UNCHANGED_PASS": true
  },
  "runtime_chain": {
    "direct_run_single_task": "passed",
    "zero_source_hotlist_metadata": "completed_or_partial_success_warning_exportable",
    "zero_source_timeout_local_fallback": "not_failed_exportable_unique_sections",
    "zero_source_no_hotlist_metadata": "failed_RESEARCH_NOT_COLLECTED",
    "accepted_source_path": "sufficient_not_hotlist_limited"
  },
  "build_result": {
    "status": "pending_rebuild",
    "exit_code": null
  }
}
```

本报告不宣布客户交付通过。
