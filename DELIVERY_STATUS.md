# Delivery Status

Branch: `fix/r2.2.21-windows-retest-defects`

Version: `RC1.3.3-Lite-R2.2.21`

Baseline: `362c6ab23fc0f0b48cf63b5855201a21cf2d7850`

R2.2.19 user retest: `FAIL`.

## R2.2.21 precise fixes

- Completed articles now expose a prominent `继续完成图文` action.
- Article confirmation leads to a selectable 1–5 image plan and paid-call confirmation.
- Image confirmation persists an independent `final_document` used by Word and ZIP export.
- Batch export remains gated until every article has a confirmed final document.
- Export failures return the failed stage, article title, safe Chinese reason, retry action, and persisted diagnostic log ID.
- Repository-level .NET SDK selection is pinned to 8.0.423 and the Windows gate records the resolved executable and installed SDKs.
- Existing R2.2.20 article → image → fusion flow is protected by regression tests.
- Issue #2/#3/#4 targeted fixes are pending full Mac and Windows validation.

## Windows CI result

- [Run 30985083335](https://github.com/329800187-design/hotsopt-article-agent/actions/runs/30985083335): .NET 8.0.423 selection, 1050-test Windows suite, security scan, launcher, portable package, installer package, inventory, and delivery gates all passed.
- The job conclusion is failed only because GitHub Actions artifact storage quota (7.53 GB across 38 retained artifacts) rejected the final evidence upload.
- No R2.2.20 installer is downloadable from that run until repository artifact capacity is released and CI is rerun.

## Pending

- Windows installed-build user retest of article → images → fusion preview → Word/ZIP.

BUILD_ALLOWED=true

READY_FOR_PACKAGE_BUILD=false

READY_FOR_USER_RETEST=false

CUSTOMER_DELIVERY_ALLOWED=false
