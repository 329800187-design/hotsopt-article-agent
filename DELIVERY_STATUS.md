# Delivery Status

Branch: `fix/r2.2.20-image-export-e2e`

Version: `RC1.3.3-Lite-R2.2.20`

Baseline: `362c6ab23fc0f0b48cf63b5855201a21cf2d7850`

R2.2.19 user retest: `FAIL`.

## R2.2.20 source repair

- Completed articles now expose a prominent `继续完成图文` action.
- Article confirmation leads to a selectable 1–5 image plan and paid-call confirmation.
- Image confirmation persists an independent `final_document` used by Word and ZIP export.
- Batch export remains gated until every article has a confirmed final document.
- Export failures return the failed stage, article title, safe Chinese reason, retry action, and persisted diagnostic log ID.
- Repository-level .NET SDK selection is pinned to 8.0.423 and the Windows gate records the resolved executable and installed SDKs.
- Mac full suite: `1035 passed, 18 skipped`; security: `SECURITY_SCAN_PASS`.

## Pending

- GitHub Windows CI package, Setup install/uninstall, and artifact gates for R2.2.20.
- Windows installed-build user retest of article → images → fusion preview → Word/ZIP.

BUILD_ALLOWED=false

READY_FOR_PACKAGE_BUILD=false

READY_FOR_USER_RETEST=false

CUSTOMER_DELIVERY_ALLOWED=false
