# Delivery Blockers

## Active Blockers

1. Final Windows Setup build is not passing.
   - Last build attempt log: `data/logs/final_p1_api_setup_build.txt`.
   - Observed blocker: missing `.NET SDK` for native launcher build.
   - Observed blocker: source package audit flags `desktop_host.py` token variable as a sensitive assignment.

2. Full customer delivery closure has not been executed from an installed Windows build.
   - Required: install, desktop launch, activation, text model test, image model test, hotspot refresh, article+two-image generation, Word export, ZIP export, restart recovery, uninstall.

3. Real image provider smoke is externally blocked.
   - No verified funded image-provider credential is available in the Mac handoff environment.
   - Minimum remaining paid validation: 2 calls for one cover image and one inline image.
   - Required closure: validate both files, export Word, export ZIP, and retain provider evidence without recording the API key.

4. Windows-only validation remains outstanding.
   - Required: DPAPI, license, native launcher, Setup, install/uninstall, single instance, restart recovery and user-data policy.

## Not Blockers

- Current text API credential was previously verified by direct and software connection after key replacement.
- P1 API integration failure was caused by test harness isolation/license setup, not article generation code.
- Freeze baseline tag remains intact.
- Provider Registry, image request adapters, asynchronous polling, cancellation/timeout, two-image lifecycle and export integration are implemented and covered by tests.
- Live hotspot target passed with 200 deduplicated current-cycle topics and zero cached entries counted.
- Annotated freeze tag is restored and peels to `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`.
- Mac full suite passed: `921 passed, 18 skipped`.
- Mac security scan passed with no forbidden hits.

## External Blockers

- Windows-only DPAPI, installed-user-data, license-gated API, Setup and launcher validation remain external to the Mac closure run.

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
