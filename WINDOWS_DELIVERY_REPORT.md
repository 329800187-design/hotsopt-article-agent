# Windows 交付验证报告

分支: `fix/r1.3-customer-delivery-final`
最终自动构建时间: 2026-07-31 10:08 CST
最终自动构建环境: GitHub Actions Windows 2025 x64

## 构建环境

| 组件 | 状态 |
|------|------|
| Python | 3.11.9 ✅ |
| Inno Setup | Chocolatey Inno Setup 6 ✅ |
| .NET SDK | 8.0.x，两个 WinExe 项目均成功编译 ✅ |
| WebView2 Bootstrapper | 微软 Evergreen 官方链接下载并校验 MZ/SHA256 ✅ |

## 修复

| 文件 | 问题 | 处理 |
|------|------|------|
| `hot_sources/base.py` | Mac 提交 61c1f7d 将其整个文件替换为一个 commit hash | 从 da8f214 恢复 |
| `scripts/package_phase1.py` | 运行时令牌读取被误报，同时存在整文件豁免 | 改为仅允许两种精确的运行时令牌赋值表达式；其他硬编码密钥继续阻断 |

## 测试结果

| 步骤 | 结果 |
|------|------|
| compileall | PASS |
| GitHub Actions pytest 全量 | 941 passed, 0 failed, 3 skipped |
| 历史本机记录 | 曾记录 2 个失败，但未附测试名或日志，不能作为最终证据 |
| 安全扫描 | SECURITY_SCAN_PASS, forbidden_hits=[] |
| 自动化构建门禁 | 9/9 PASS |

## 构建产物

| 文件 | 大小 | SHA256 |
|------|------|--------|
| 热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1-R1.2_Source.zip | CI 产物 | `3b0e50ab7e8cf7b8bdcd19c5d70779afb140b9dd6d0aa259c281b122db6b3eeb` |
| hotspot-article-agent-windows-ci-windows.zip | CI 产物 | `31271c8ec69203049b36ccee32851d1ef197be26b7f27a716b760c33e8b6ced7` |
| 热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1-R1.2_Setup.exe | 87,446,666 bytes | `cff230bee302b529ca341b01e976d2b3a64b9e5fa6ef63f37a937e4ad6a19439` |

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
- GitHub Actions 最终绿色证据：run `30597712637`，提交 `876ac9e553e1e46273fe671732cd1bd54dea77de`。
- 自动化测试、构建、安装和卸载已通过；真实文本 API、真实两图、DPAPI 进程重启、许可证真实激活及人工 Word/ZIP 检查尚未执行。
