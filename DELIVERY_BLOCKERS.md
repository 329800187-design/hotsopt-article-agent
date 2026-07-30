# Delivery Blockers

## Active Blockers

1. Final Windows Setup build is not passing.
   - Last build attempt log: `data/logs/final_p1_api_setup_build.txt`.
   - Observed blocker: missing `.NET SDK` for native launcher build.
   - Observed blocker: source package audit flags `desktop_host.py` token variable as a sensitive assignment.

2. Full customer delivery closure has not been executed from an installed Windows build.
   - Required: install, desktop launch, activation, text model test, image model test, hotspot refresh, article+two-image generation, Word export, ZIP export, restart recovery, uninstall.

3. Provider Registry first implementation completed.
   - Remaining work is request-side provider adaptation, async image execution,
     two-image task lifecycle, real image smoke and final delivery closure.

4. Image generation closure P0-C is not verified.
   - Need two real images, file validation, retry isolation, and image result consistency.

5. Hotspot pool target is not verified.
   - Required deduplicated usable hotspots: `>= 200`.
   - Current real Mac refresh: 145 usable topics from 3 responsive providers.
   - Toutiao official returned 404; NewsNow returned a non-JSON response; several TopHub boards returned no parseable rows.

6. The expected freeze tag is not visible in the remote GitHub refs API.
   - Expected: `freeze-r1.2-before-research-fix` -> `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`.
   - No tag has been created, moved, or rewritten during this handoff.

## Not Blockers

- Current text API credential was previously verified by direct and software connection after key replacement.
- P1 API integration failure was caused by test harness isolation/license setup, not article generation code.
- Freeze baseline tag remains intact.

## External Blockers

- If real image provider key, quota, or model permission is unavailable, record it here and keep `CUSTOMER_DELIVERY_ALLOWED=false`.
- Windows-only DPAPI, installed-user-data, license-gated API, Setup and launcher validation remain external to the Mac closure run.
