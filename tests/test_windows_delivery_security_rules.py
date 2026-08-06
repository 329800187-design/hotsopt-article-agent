from pathlib import Path

from scripts import package_phase1


def test_exact_runtime_token_env_read_is_allowed() -> None:
    source = '            token = os.environ.get("HOTSPOT_LOCAL_API_TOKEN", "")\n'
    assert package_phase1.scan_text(Path("desktop_host.py"), source) == []


def test_runtime_authorization_header_composition_is_allowed() -> None:
    source = 'headers = {"Authorization": f"Bearer {token}"}\n'
    assert package_phase1.scan_text(Path("desktop_host.py"), source) == []


def test_hardcoded_openai_key_in_desktop_host_is_forbidden() -> None:
    source = 'api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"\n'
    assert {"openai_key", "key_assignment"} <= set(
        package_phase1.scan_text(Path("desktop_host.py"), source)
    )


def test_hardcoded_bearer_value_in_desktop_host_is_forbidden() -> None:
    source = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n'
    assert "bearer_token" in package_phase1.scan_text(Path("desktop_host.py"), source)


def test_plaintext_api_key_in_desktop_host_is_forbidden() -> None:
    source = "api_key = plaintext_customer_secret\n"
    assert "key_assignment" in package_phase1.scan_text(Path("desktop_host.py"), source)
