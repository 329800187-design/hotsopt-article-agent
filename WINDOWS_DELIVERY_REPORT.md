# Windows 交付验证报告

分支: `fix/r1.3-customer-delivery-final`
构建时间: 2026-07-31 CST
机器: i7-11800H / RTX 3060 / Windows 11

## 构建环境

| 组件 | 状态 |
|------|------|
| Python | 3.11.15 ✅ |
| Inno Setup | 6.7.3 (winget, %LOCALAPPDATA%\Programs\Inno Setup 6\) ✅ |
| .NET SDK | 未安装，降级到 csc.exe (v4.0.30319) ✅ |
| WebView2 Bootstrapper | 从原项目复制 ✅ |

## 修复

| 文件 | 问题 | 处理 |
|------|------|------|
| `hot_sources/base.py` | Mac 提交 61c1f7d 将其整个文件替换为一个 commit hash | 从 da8f214 恢复 |
| `scripts/package_phase1.py` | 运行时令牌读取被误报，同时存在整文件豁免 | 改为仅允许两种精确的运行时令牌赋值表达式；其他硬编码密钥继续阻断 |

## 测试结果

| 步骤 | 结果 |
|------|------|
| compileall | PASS |
| GitHub Actions pytest 全量 | 936 passed, 0 failed, 3 skipped |
| 历史本机记录 | 曾记录 2 个失败，但未附测试名或日志，不能作为最终证据 |
| 安全扫描 | 待修复后的 Windows CI 复验 |

## 构建产物

| 文件 | 大小 | SHA256 |
|------|------|--------|
| 热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1-R1.2_Source.zip | 2.2 MB | 见 manifest |
| 热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1-R1.2_Windows运行包.zip | 91.9 MB | 见 manifest |
| 热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1-R1.2_Setup.exe | 59 MB | 见 manifest |

## 安装/卸载验证

| 检查项 | 结果 |
|--------|------|
| INNO_SETUP_INSTALL_PASS | ✅ true |
| WINDOWS_APPS_ENTRY_PASS | ✅ true |
| INNO_UNINSTALL_REAL_PASS | ✅ true |
| INSTALL_DIR_REMOVED_PASS | ✅ true |
| USER_DATA_PRESERVED_PASS | ✅ true |
| install_returncode | 0 |
| uninstall_returncode | 0 |

## 安装后启动验证

| 检查项 | 结果 |
|--------|------|
| is_installed() | ✅ True |
| config_dir | %LOCALAPPDATA%\热点图文批量生产工作台\config |
| API /api/health | ✅ 200 |
| 数据库路径 | 用户数据目录，非安装目录 |
| 版本 | RC1.3.3-Lite-P1-HF4.1-R1.2 |

## 备注

- `/VERYSILENT` 参数对此 Setup 不生效（Inno Setup 向导未编译为可静默模式），需手动点击安装
- 缺少 .NET SDK 不影响构建，csc.exe 降级方案正常工作
- 本报告不宣布客户交付通过。BUILD_ALLOWED=false, CUSTOMER_DELIVERY_ALLOWED=false
- GitHub Actions 证据：run 30593120499 的测试为 936 passed、3 skipped；构建门禁仍失败，修复后须以新 run 为准。
