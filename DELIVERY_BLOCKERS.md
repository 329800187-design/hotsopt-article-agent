# Delivery Blockers

## Current blockers

1. R2.2.20 has not yet completed GitHub Windows CI and Setup build/install/uninstall validation.
2. The customer has not yet retested the complete installed flow: article confirmation, 1–5 images, image confirmation, final-document preview, Word, and ZIP.
3. A real exported DOCX must be opened on Windows and verified to contain the article body and selected images.

## Mac evidence

- compileall: PASS
- pytest: `1035 passed, 18 skipped`
- R2.2.20 image/export E2E: `13 passed`
- security: `SECURITY_SCAN_PASS`, `forbidden_hits=[]`

BUILD_ALLOWED=false

READY_FOR_PACKAGE_BUILD=false

READY_FOR_USER_RETEST=false

CUSTOMER_DELIVERY_ALLOWED=false
