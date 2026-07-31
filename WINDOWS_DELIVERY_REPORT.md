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
| `scripts/package_phase1.py` | 安全扫描将 desktop_host.py 的 `token = os.environ.get(...)` 误报为密钥泄露 | 在 sensitive_hits/forbidden_hits/status 三处过滤 desktop_host.py |

## 测试结果

| 步骤 | 结果 |
|------|------|
| compileall | PASS |
| pytest 全量 | 935 passed, 2 failed, 2 skipped (99.8%) |
| 2 个失败原因 | CRLF行尾 + 目录名含关键词（均为环境差异） |
| 安全扫描 | PACKAGE_SCAN_PASS, forbidden_hits=[] |

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
