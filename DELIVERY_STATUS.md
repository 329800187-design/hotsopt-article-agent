# Delivery Status

Current branch: `fix/r1.3-customer-delivery-final`

Current HEAD: `da8f214de3aa770c79eb94f208c22ea3c1ffbe2d`

Freeze baseline: `freeze-r1.2-before-research-fix` -> `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`

Current phase: Mac closure complete; Windows installation and real-image handoff

## Completed

- Preserved freeze baseline and recorded initial Git state in `data/logs/delivery_final_initial_git_state.txt`.
- Continued from latest valid Research Fix/P1 API branch rather than restarting from freeze.
- Created delivery branch `fix/r1.3-customer-delivery-final`.
- Added delivery tracking files: `DELIVERY_STATUS.md`, `DELIVERY_BLOCKERS.md`, `NEXT_COMMAND.txt`.
- Implemented centralized Provider Registry in `providers/registry.py`.
- Moved default text/image provider profile generation to `modules/config_store.py`.
- Moved UI provider preset source to Registry via `ui/rc1_app.py`.
- Added image response adapter support for OpenAI `b64_json`, URL fetch, plain base64 fields, Data URI payloads, Dashscope native `image`, and post-write raster validation.
- Wired Registry request/response adapters into image runtime profiles and prevented duplicated version paths such as `/v1/v1/images/generations`.
- Added submit-once asynchronous image tasks, normalized `task_id`/`request_id`/`job_id`, bounded polling, controlled backoff, terminal-state mapping and cancellation checks.
- Preserved cancellation through cover and inline generation, retained compatibility with existing provider test doubles, and kept Registry-backed provider labels visible to UI audits.
- Expanded the default hotspot source set across Toutiao, Weibo, Zhihu, Baidu, Bilibili and Douyin TopHub boards; an unconfigured custom DailyHot endpoint no longer prevents fallback sources from starting.
- Added per-provider health diagnostics, multi-platform TopHub overview ingestion, stronger live-topic normalization/deduplication, and reproducible final hotspot evidence.
- Restored annotated tag `freeze-r1.2-before-research-fix`; peeled target is `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`.
- Completed real-network hotspot refresh with 431 live topics before global deduplication and 200 after deduplication; all 200 have URL/source identifiers and current-cycle capture timestamps, with zero cached topics counted.
- Completed final Mac full suite with raw environment and pytest logs.
- Completed final Mac security scan with `SECURITY_SCAN_PASS` and no forbidden hits.
- P1 API integration blocker previously resolved: `4 passed, 6 deselected`.
- Production license gate tests previously rerun: `12 passed`.
- Security scan previous result: `SECURITY_SCAN_PASS`.
- Real text smoke evidence exists for two articles with `used_local_fallback=false` and empty `fallback_kind`.

## Not Completed

- Real image provider smoke with two validated generated images.
- Final installed Windows customer flow: install, launch, activation, model setup, article+image generation, Word/ZIP export, restart recovery, uninstall.
- Final Setup and customer delivery ZIP.

## Recent Test Results

- `compileall`: PASS on `providers`, `generation`, `modules`, `ui`, and new provider tests.
- P0-A Registry/config targeted: `16 passed`.
- P0-C image adapter/provider targeted: `18 passed` including provider registry and image budget smoke.
- P0-C2 async/two-image targeted: `36 passed`.
- Final Mac full suite: `921 passed, 18 skipped, 1 warning` in 54.72 seconds; exit code 0.
- The five newly explicit Mac skips are Windows DPAPI credential-persistence tests.
- Export targeted: `43 passed`.
- Hotspot targeted: `101 passed`.
- Final live hotspot evidence: 431 before deduplication, 200 after deduplication, 200 with URL/source identifier, 200 with capture time, 0 cached.
- Article/research targeted after Provider Registry: `57 passed`.
- RC: last known `307 passed, 2 skipped`.
- P1 non-API: last known `258 passed, 4 deselected`.
- P1 API: `4 passed, 6 deselected`.
- other: final log shows `135 passed`.
- phase: last known `208 passed`.
- Final security scan: `SECURITY_SCAN_PASS`, `forbidden_hits=[]`, 339 files scanned.

## Recent Real Smoke

- Smoke A: body Chinese count `1091`, model call succeeded, Word export succeeded.
- Smoke B: rewritten body Chinese count `1463`, model call succeeded, Word export succeeded.
- Image real smoke for current final delivery branch: not completed.

## Windows Handoff Checklist

1. Pull `fix/r1.3-customer-delivery-final` and verify the restored freeze tag.
2. Confirm the .NET SDK and repair the native launcher build.
3. Resolve the `desktop_host.py` package-audit false positive without weakening the real secret gate.
4. Run the complete Windows test suite and verify DPAPI and license flows.
5. Build Setup; install; launch from the desktop; activate; save and test text/image model profiles.
6. Refresh at least 200 live hotspots.
7. Generate one real article with one cover and one inline image; validate both images.
8. Export Word and ZIP; restart and verify history recovery and single-instance behavior.
9. Uninstall and verify the intended user-data retention policy.

## Next Command

`git fetch --all --tags --prune`

## Gates

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
