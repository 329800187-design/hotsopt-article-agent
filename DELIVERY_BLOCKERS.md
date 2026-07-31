# Delivery Blockers

## Active Blockers

1. The repaired Windows CI build gates need a green rerun.
   - Run `30593120499` proved `936 passed, 0 failed, 3 skipped`.
   - It failed launcher, portable-package and Inno gates; fixes now require Windows-runner verification.

2. Installed-build credential and license persistence remain unverified.
   - Required: DPAPI text/image credential restart recovery and the real license activation matrix.

3. Real image provider smoke is externally blocked.
   - No verified funded image-provider credential is available in the Mac handoff environment.
   - Minimum remaining paid validation: 2 calls for one cover image and one inline image.
   - Required closure: validate both files, export Word, export ZIP, and retain provider evidence without recording the API key.

4. Final installed customer flow remains outstanding.
   - Required: latest-Setup real article/two-image smoke, manual Word/ZIP inspection, single instance and restart recovery.

5. The final Setup must be rebuilt after all preceding gates pass.

## Not Blockers

- Current text API credential was previously verified by direct and software connection after key replacement.
- P1 API integration failure was caused by test harness isolation/license setup, not article generation code.
- Freeze baseline tag remains intact.
- Provider Registry, image request adapters, asynchronous polling, cancellation/timeout, two-image lifecycle and export integration are implemented and covered by tests.
- Live hotspot target passed with 200 deduplicated current-cycle topics and zero cached entries counted.
- Annotated freeze tag is restored and peels to `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`.
- Mac full suite passed: `921 passed, 18 skipped`.
- Mac security scan passed with no forbidden hits.
- A prior Windows machine report records Setup build, install, uninstall and API health 200.
- Windows Actions full suite has zero failures; the unsupported historical “2 failures” claim is not an active blocker.
- The exact package security allow rule is implemented locally with no whole-file exemption and currently reports `SECURITY_SCAN_PASS`.

## External Blockers

- Windows-only DPAPI, real license, paid image calls and manual installed-app checks require the Windows/customer test environment.

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
