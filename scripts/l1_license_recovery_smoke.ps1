$ErrorActionPreference = "Stop"
$root = (Get-Location).Path
$dataRoot = Join-Path $env:TEMP ("hotspot-l1-rc1-2-3-recovery-" + $PID)
$scriptPath = Join-Path $env:TEMP ("hotspot-l1-rc1-2-3-recovery-" + $PID + ".py")
$env:HOTSPOT_DATA_ROOT = $dataRoot
$env:PYTHONPATH = $root
$code = @'
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from license_admin.license_generator import create_license, write_license
from modules import device_identity, license_service

data_root = Path(os.environ["HOTSPOT_DATA_ROOT"])
candidate = data_root / "recovery-test.license"
first_code = device_identity.device_code()
installation_json = device_identity.installation_path()
installation_backup = device_identity.installation_backup_path()
if not installation_json.exists() or not installation_backup.exists():
    raise SystemExit("installation pair was not created")
installation_json.unlink()
if device_identity.device_code() != first_code:
    raise SystemExit("device code changed after JSON recovery")
print("INSTALLATION_ID_RECOVERY_PASS")

now = datetime.now(timezone.utc).replace(microsecond=0)
license_value = create_license(
    customer_name="RC1.2 Recovery Smoke",
    device_code=first_code,
    license_id="RC12-RECOVERY-SMOKE",
    not_before=(now - timedelta(minutes=1)).isoformat(),
    expires_at=(now + timedelta(days=30)).isoformat(),
)
write_license(license_value, candidate)
if not license_service.import_license(candidate).get("valid"):
    raise SystemExit("license import failed")

previous = now + timedelta(days=2)
license_service.save_secret(license_service.STATE_SECRET_NAME, previous.isoformat(), path=license_service._state_secret_path())
if license_service.check_license(now=now).get("code") != "CLOCK_ROLLBACK_SUSPECTED":
    raise SystemExit("rollback was not detected")
blocked_first = license_service.check_system_time(now=now + timedelta(seconds=10))
blocked_second = license_service.check_system_time(now=now + timedelta(seconds=20))
if blocked_first.get("recovery_check_count") != 0 or blocked_second.get("recoverable"):
    raise SystemExit("uncorrected clock was not blocked")
if license_service.recover_clock_rollback(now=now + timedelta(seconds=20)).get("recovered"):
    raise SystemExit("uncorrected clock incorrectly recovered")
print("CLOCK_ROLLBACK_STILL_BLOCKED_PASS")

corrected_time = previous + timedelta(seconds=10)
if not corrected_time >= previous:
    raise SystemExit("corrected time did not reach the trusted reference")
if license_service.check_system_time(now=corrected_time).get("recovery_check_count") != 1:
    raise SystemExit("corrected time did not start recovery")
if not license_service.check_system_time(now=corrected_time + timedelta(seconds=10)).get("recovery_ready"):
    raise SystemExit("corrected time did not become ready after two checks")
if not license_service.recover_clock_rollback(now=corrected_time + timedelta(seconds=10)).get("recovered"):
    raise SystemExit("clock recovery failed")
if not license_service.check_license(now=corrected_time + timedelta(seconds=20)).get("valid"):
    raise SystemExit("license did not recover")
if not license_service.license_allows_generation("five_articles", now=corrected_time + timedelta(seconds=20))[0]:
    raise SystemExit("generation permission did not recover")
print("CLOCK_CORRECTED_TIME_RECOVERY_PASS")
print("OFFLINE_LICENSE_RC1_2_3_SELF_REVIEW_PASS")
'@
[IO.File]::WriteAllText($scriptPath, $code, [Text.UTF8Encoding]::new($false))
try {
    & python $scriptPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    if (Test-Path -LiteralPath $scriptPath) { [IO.File]::Delete($scriptPath) }
    if (Test-Path -LiteralPath $dataRoot) { [IO.Directory]::Delete($dataRoot, $true) }
}
