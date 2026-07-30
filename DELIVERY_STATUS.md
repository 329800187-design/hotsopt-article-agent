# Delivery Status

Current branch: `fix/r1.3-customer-delivery-final`

Current HEAD: `9e5e98af7650379ef5b2dfb213c2e3eef4e8d552`

Freeze baseline: `freeze-r1.2-before-research-fix` -> `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`

Current phase: P0-C2 image request, async execution, two-image lifecycle and export closure

## Completed

- Preserved freeze baseline and recorded initial Git state in `data/logs/delivery_final_initial_git_state.txt`.
- Continued from latest valid Research Fix/P1 API branch rather than restarting from freeze.
- Created delivery branch `fix/r1.3-customer-delivery-final`.
- Added delivery tracking files: `DELIVERY_STATUS.md`, `DELIVERY_BLOCKERS.md`, `NEXT_COMMAND.txt`.
- Implemented centralized Provider Registry in `providers/registry.py`.
- Moved default text/image provider profile generation to `modules/config_store.py`.
- Moved UI provider preset source to Registry via `ui/rc1_app.py`.
- Added image response adapter support for OpenAI `b64_json`, URL fetch, plain base64 fields, Data URI payloads, Dashscope native `image`, and post-write raster validation.
- P1 API integration blocker previously resolved: `4 passed, 6 deselected`.
- Production license gate tests previously rerun: `12 passed`.
- Security scan previous result: `SECURITY_SCAN_PASS`.
- Real text smoke evidence exists for two articles with `used_local_fallback=false` and empty `fallback_kind`.

## Not Completed

- Real image provider smoke with two validated generated images.
- Hotspot pool verification with at least 200 deduplicated usable topics.
- Final installed Windows customer flow: install, launch, activation, model setup, article+image generation, Word/ZIP export, restart recovery, uninstall.
- Final Setup and customer delivery ZIP.
- Final full test suite run after all delivery changes.

## Recent Test Results

- `compileall`: PASS on `providers`, `generation`, `modules`, `ui`, and new provider tests.
- P0-A Registry/config targeted: `16 passed`.
- P0-C image adapter/provider targeted: `16 passed` including provider registry and image budget smoke.
- Article/research targeted after Provider Registry: `57 passed`.
- RC: last known `307 passed, 2 skipped`.
- P1 non-API: last known `258 passed, 4 deselected`.
- P1 API: `4 passed, 6 deselected`.
- other: final log shows `135 passed`.
- phase: last known `208 passed`.
- security scan: last known `SECURITY_SCAN_PASS`.

## Recent Real Smoke

- Smoke A: body Chinese count `1091`, model call succeeded, Word export succeeded.
- Smoke B: rewritten body Chinese count `1463`, model call succeeded, Word export succeeded.
- Image real smoke for current final delivery branch: not completed.

## Next Command

`python -m compileall providers generation modules ui`

## Gates

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
