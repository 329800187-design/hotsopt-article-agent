"""Dev-mode backend launcher — replicates desktop_host environment without pythonw.exe requirement."""
import os, sys, subprocess

PROJECT_ROOT = r"E:\hotspot-article-agent"
DATA_ROOT = os.path.join(PROJECT_ROOT, "data")
API_PORT = "8506"
WEB_PORT = "8505"

os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

os.environ["HOTSPOT_DATA_ROOT"] = DATA_ROOT
os.environ["HOTSPOT_DESKTOP"] = "1"
os.environ["HOTSPOT_NO_BROWSER"] = "1"
os.environ["HOTSPOT_API_PORT"] = API_PORT
os.environ["HOTSPOT_WEB_PORT"] = WEB_PORT
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# Initialize DB (same as desktop_host.prepare_environment)
from modules.database import init_db
init_db()

# Generate/read token
from modules.local_api_token import get_or_create_token
token = get_or_create_token()
os.environ["HOTSPOT_LOCAL_API_TOKEN"] = token
print(f"TOKEN_SET length={len(token)}", flush=True)

# Start API
api_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", API_PORT],
    env=os.environ.copy(),
    cwd=PROJECT_ROOT,
)
print(f"API_PID={api_proc.pid} PORT={API_PORT}", flush=True)

# Wait for API health
import time, urllib.request
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{API_PORT}/api/health",
            headers={"X-Hotspot-Token": token}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                print("API_HEALTH_PASS", flush=True)
                break
    except Exception:
        pass
    if api_proc.poll() is not None:
        print(f"API_DIED exit_code={api_proc.poll()}", flush=True)
        sys.exit(1)
    time.sleep(1)
else:
    print("API_HEALTH_TIMEOUT", flush=True)
    sys.exit(1)

# Start Streamlit
web_proc = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", "app.py",
     "--server.address", "127.0.0.1", "--server.headless", "true",
     "--server.port", WEB_PORT, "--browser.gatherUsageStats", "false"],
    env=os.environ.copy(),
    cwd=PROJECT_ROOT,
)
print(f"WEB_PID={web_proc.pid} PORT={WEB_PORT}", flush=True)

# Wait for Streamlit
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}", timeout=2) as resp:
            if resp.status == 200:
                print("WEB_HEALTH_PASS", flush=True)
                break
    except Exception:
        pass
    time.sleep(1)
else:
    print("WEB_HEALTH_TIMEOUT", flush=True)

print("READY", flush=True)

# Keep running until interrupted
try:
    api_proc.wait()
except KeyboardInterrupt:
    pass
finally:
    for p in [api_proc, web_proc]:
        try:
            p.terminate()
        except:
            pass
