# Windows 交付验证报告

分支：`fix/r1.3-customer-delivery-final`

版本：`RC1.3.3-Lite-R2.2.8`

状态：Issue #1 Mac 自动化修复与 Windows CI 完成；Windows Hermes 真实许可证闭环待执行。

## Issue #1 修复

| 项目 | 结果 |
|---|---|
| 正式数据目录 | `%LOCALAPPDATA%\热点图文批量生产工作台` |
| 旧目录迁移 | 仅在正式目录没有完整身份且旧身份唯一、完整、一致时，暂存复制、校验并原子切换 |
| 冲突保护 | 正式身份不覆盖；旧身份不完整或不一致时停止并报告错误码 |
| 设备诊断 | 显示启动模式、数据路径、可写性、迁移状态和安全错误码 |
| 签发入口 | 中文 EXE → 英文兼容 EXE → `.venv` → `py -3` → 系统 Python |
| 签发预检 | 私钥缺失/损坏/算法错误、公钥缺失/损坏、密钥不匹配均拒绝签发 |
| 版本元数据 | `modules/app_metadata.py` 单一权威来源 |

## Mac 自动化

| 检查 | 结果 |
|---|---|
| compileall | PASS |
| pytest 全量 | 938 passed, 0 failed, 18 skipped |
| 迁移定向测试 | 4 passed |
| 签发定向测试 | 4 passed |
| 安全扫描 | SECURITY_SCAN_PASS；forbidden_hits=[] |

证据位于 `data/logs/issue_1_mac_*.txt/json` 和 `data/logs/issue_1_*_tests.txt`。

## Windows CI

[Windows Delivery CI run 30622596471（attempt 2）](https://github.com/329800187-design/hotsopt-article-agent/actions/runs/30622596471) 已通过：

- pytest：`953 passed, 0 failed, 3 skipped`
- `SECURITY_SCAN_PASS`
- 原生 Launcher：PASS
- Portable package：PASS
- Inno Setup build/install/uninstall：PASS
- Windows 已安装应用入口：PASS
- 卸载保留用户数据：PASS

## PENDING_WINDOWS_REAL_VALIDATION

- Windows 源码启动设备码。
- Windows 安装版设备码。
- 设备码跨完整进程重启稳定。
- MachineGuid/CIM 与 DPAPI 真实行为。
- 原始生产私钥恢复及真实公私钥匹配。
- 真实许可证签发、客户端激活、重启保持。
- 受限模式解除。
- 真实文本与两图验收。

Issue #1 保持打开。

BUILD_ALLOWED=false

CUSTOMER_DELIVERY_ALLOWED=false
