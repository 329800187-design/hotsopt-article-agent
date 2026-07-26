# RC1.3.2 客户 Windows 包文件白名单

## 允许进入客户包

- 运行入口：`api.py`、`app.py`、`launcher.ps1`、`start.bat`、`start.vbs`、`stop.bat`、`create_shortcut.ps1`。
- 运行源码：`modules/`、`generation/`、`providers/`、`hot_sources/`、`export/`、`ui/`。
- 必要脚本：`scripts/python_runtime.ps1`、`scripts/stop_project.ps1`。
- 配置样例和依赖锁：`config/settings.example.json`、`requirements.txt`、`requirements-runtime.txt`。
- 法务说明：`LICENSE`、`THIRD_PARTY_NOTICES.md`、`RC1_WINDOWS_README.md`。
- 内置运行时：`runtime/python.exe` 与运行所需精简依赖。

## 禁止进入客户包

- 本机敏感文件：`config/settings.json`、`config/credentials.dat`、`runtime/local-api-token.dat`。
- 测试/开发文件：`tests/`、`pytest.ini`、`requirements-dev.txt`、`.gitignore`、`install.bat`。
- 开发脚本和历史证据：`scripts/package_rc1.py`、`scripts/phase*_smoke*.py`、`scripts/security_scan.py`、历史阶段报告。
- 运行产物：`data/`、`logs/`、`outputs/`、`.venv/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`。
- Runtime 开发资产：`idlelib`、`lib2to3`、`turtledemo`、`venv`、`ensurepip`、`*.pyi`、`*.pxd`、`*.pyx`、`*.h`、`*.c`、`testdata`、`benchmark`。

## 验收

- `scripts/rc1_3_1_customer_package_smoke.ps1` 继续作为客户包白名单烟测入口。
- Manifest 中 `dependency_errors` 必须为空，`sensitive_hits` 必须为空。
