# RC1.3.1 客户 Windows 包文件白名单

## 允许进入客户包

- 根目录运行入口：`api.py`、`app.py`、`launcher.ps1`、`start.bat`、`start.vbs`、`stop.bat`、`create_shortcut.ps1`。
- 运行源码目录：`modules/`、`generation/`、`providers/`、`hot_sources/`、`export/`、`ui/`。
- 必要脚本：`scripts/python_runtime.ps1`、`scripts/stop_project.ps1`。
- 示例配置：`config/settings.example.json`。
- 法务与说明：`LICENSE`、`THIRD_PARTY_NOTICES.md`、`RC1_WINDOWS_README.md`。
- 内置运行时：`runtime/python.exe` 与运行所需精简依赖。

## 禁止进入客户包

- 本机配置和凭据：`config/settings.json`、`config/credentials.dat`。
- 测试和开发文件：`tests/`、`pytest.ini`、`requirements-dev.txt`、`.gitignore`、`install.bat`。
- 打包/验收脚本：`scripts/package_rc1.py`、`scripts/phase*_smoke_test.py`、`scripts/security_scan.py`、`scripts/*audit*`。
- 历史报告与开发状态：`STATUS.md`、`TECH_AUDIT.md`、`docs/` 下的阶段验收报告。
- 运行产物：`data/`、`logs/`、`outputs/`、`.venv/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`。

## 验收方式

- `scripts/package_rc1.py` 按白名单生成 Windows 包。
- `scripts/rc1_3_1_customer_package_smoke.ps1` 解压最终 Windows 包并检查禁止项、必需项、`.pyc=0` 和 `settings.json=0`。
