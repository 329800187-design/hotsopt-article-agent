# Delivery Blockers

## Current blockers

1. GitHub Actions artifact storage quota is full, so run 30985083335 could not upload its successfully built R2.2.20 installer and evidence.
2. The customer has not yet retested the complete installed flow: article confirmation, 1–5 images, image confirmation, final-document preview, Word, and ZIP.
3. A real exported DOCX must be opened on Windows and verified to contain the article body and selected images.

## Mac evidence

- compileall: PASS
- pytest: `1035 passed, 18 skipped`
- R2.2.20 image/export E2E: `15 passed`
- security: `SECURITY_SCAN_PASS`, `forbidden_hits=[]`

## Windows CI evidence

- Run 30985083335: Windows pytest `1050 passed, 3 skipped`; security `PASS`.
- .NET SDK 8.0.423, launcher, portable package, installer package, inventory, and all delivery gates: `PASS`.
- Final artifact upload: `FAIL` (`Artifact storage quota has been hit`).

BUILD_ALLOWED=true

READY_FOR_PACKAGE_BUILD=true

READY_FOR_USER_RETEST=false

CUSTOMER_DELIVERY_ALLOWED=false
