# Delivery Status

Current branch: `fix/r1.3-customer-delivery-final`

Current HEAD: `be4803f7ea4988474dfc5f66811828dd62c7dc91`

Freeze baseline: `freeze-r1.2-before-research-fix` -> `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`

Current phase: customer-usable delivery closure, initial state captured.

## Completed

- Preserved freeze baseline and recorded initial Git state in `data/logs/delivery_final_initial_git_state.txt`.
- Continued from latest valid Research Fix/P1 API branch rather than restarting from freeze.
- P1 API integration blocker previously resolved: `4 passed, 6 deselected`.
- Production license gate tests previously rerun: `12 passed`.
- Security scan previous result: `SECURITY_SCAN_PASS`.
- Real text smoke evidence exists for two articles with `used_local_fallback=false` and empty `fallback_kind`.
- Latest source commit already pushed to GitHub on `fix/r1.2-research-fact-cards`.

## Not Completed

- Provider Registry refactor for independent text/image provider profiles.
- Full image generation closure with two validated images per default article.
- Hotspot pool verification with at least 200 deduplicated usable topics.
- Final installed Windows customer flow: install, launch, activation, model setup, article+image generation, Word/ZIP export, restart recovery, uninstall.
- Final Setup and customer delivery ZIP.
- Final full test suite run after all delivery changes.

## Recent Test Results

- `compileall`: last known PASS before final delivery branch.
- RC: last known `307 passed, 2 skipped`.
- P1 non-API: last known `258 passed, 4 deselected`.
- P1 API: `4 passed, 6 deselected`.
- other: final log shows `135 passed`.
- phase: last known `208 passed`.
- security scan: `SECURITY_SCAN_PASS`.

## Recent Real Smoke

- Smoke A: body Chinese count `1091`, model call succeeded, Word export succeeded.
- Smoke B: rewritten body Chinese count `1463`, model call succeeded, Word export succeeded.
- Image real smoke for current final delivery branch: not completed.

## Next Command

`python -m compileall -q api.py providers generation modules scripts tests`

## Gates

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
