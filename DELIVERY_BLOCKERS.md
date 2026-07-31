# Delivery Blockers

## Active blockers

1. `PENDING_WINDOWS_REAL_VALIDATION`: source checkout must generate and retain one device code as a standard Windows user.
2. `PENDING_WINDOWS_REAL_VALIDATION`: installed build must use the same official LocalAppData root and retain its device code across full restarts.
3. `PENDING_WINDOWS_REAL_VALIDATION`: real DPAPI and MachineGuid/CIM behavior cannot be proven on Mac.
4. The original production signing private key must be restored outside Git and match the shipped client public key.
5. A real license must be signed, activated by the client, and remain active after restart.
6. Restricted mode must unlock model configuration only after valid activation.
7. Real text generation and two generated images still require Windows/customer acceptance.

## Automated evidence

- Mac compileall: PASS.
- Mac pytest: `938 passed, 18 skipped`.
- Mac security scan: `SECURITY_SCAN_PASS`, no forbidden hits.
- Migration tests: `4 passed`.
- Signer tests: `4 passed`.
- Windows CI run `30622596471` attempt 2: `953 passed, 3 skipped`; security, launcher, portable, Inno Setup, install, uninstall, and preserved-user-data gates passed.

Issue #1 must remain open until the real Windows activation closure is complete.

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
