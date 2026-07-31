# Delivery Status

Branch: `fix/r1.3-customer-delivery-final`

Version: `RC1.3.3-Lite-R2.2.8`

Freeze baseline: `freeze-r1.2-before-research-fix` -> `0f827b4e54f8018a0cbdf71cabfca60c07f10c18`

Current phase: Issue #1 automated repair complete on Mac; Windows CI and real Windows activation validation pending.

## Issue #1 completed locally

- Unified every normal Windows entry point on `%LOCALAPPDATA%\热点图文批量生产工作台`.
- Added guarded, identity-preserving migration from the old short LocalAppData directory and repository `data`.
- Added stable device-identity error codes and safe diagnostics without identity, license, credential, or key material.
- Repaired signer launch priority: Chinese EXE, legacy English EXE, `.venv`, `py -3`, then system Python.
- Added signer private/public key preflight and strict mismatch rejection.
- Centralized release metadata in `modules/app_metadata.py`.
- Mac full suite: `938 passed, 18 skipped`; security: `SECURITY_SCAN_PASS`.

## Pending Windows real validation

- Source and installed device-code generation under a standard non-admin account.
- Device-code stability across complete process restart.
- Real DPAPI persistence and MachineGuid/CIM fallback.
- Original production private-key restoration and real public/private match.
- Real license signing, client activation, restricted-mode release, text generation, and two-image acceptance.

Issue #1 remains open.

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
