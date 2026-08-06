$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $root "hotspot-license-admin-l1-rc1-2-3.zip"
if (-not (Test-Path -LiteralPath $zip)) { throw "Admin package not found" }
$private = $env:HOTSPOT_LICENSE_PRIVATE_KEY
if ([string]::IsNullOrWhiteSpace($private)) { $private = Join-Path $HOME "hotspot-license-admin\license_private_key.pem" }
if (-not (Test-Path -LiteralPath $private)) { throw "Developer signing private key not found" }
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("l1-admin-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    Expand-Archive -LiteralPath $zip -DestinationPath $temp
    $env:HOTSPOT_LICENSE_PRIVATE_KEY = $private
    $code = @'
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from license_admin.license_generator import create_license, write_license
from license_admin.license_schema import canonical_payload
import base64
now = datetime.now(timezone.utc).replace(microsecond=0)
value = create_license(customer_name="Admin Smoke", device_code="AAAA-BBBB-CCCC-DDDD-EEEE", license_id="ADMIN-SMOKE-0001", not_before=(now-timedelta(minutes=1)).isoformat(), expires_at=(now+timedelta(days=1)).isoformat())
payload = canonical_payload(value)
signature = base64.urlsafe_b64decode(value["signature"] + "=" * (-len(value["signature"]) % 4))
public = serialization.load_pem_public_key(Path("resources/license_public_key.pem").read_bytes())
assert isinstance(public, Ed25519PublicKey)
public.verify(signature, payload)
write_license(value, Path("admin-smoke.license"))
print("ADMIN_LICENSE_SMOKE_PASS")
'@
    $script = Join-Path $temp "admin_smoke.py"
    [System.IO.File]::WriteAllText($script, $code, (New-Object System.Text.UTF8Encoding($false)))
    Push-Location $temp
    try { & python $script } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "admin package smoke failed" }
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
