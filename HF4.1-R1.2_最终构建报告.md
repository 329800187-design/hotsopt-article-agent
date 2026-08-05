# RC1.3.3-Lite-R2.2.20 构建状态报告

构建时间：2026-08-01T18:12:25.601736+00:00

当前状态：`Windows CI run 30985083335 构建与全部门禁通过；GitHub Actions 制品额度已满导致最终上传失败，当前无可下载的 R2.2.20 安装包。`

## 结论

- Setup 与卸载统一使用 Inno Setup 的 `unins000.exe`。
- 安装目录统一为 `%LOCALAPPDATA%\Programs\热点图文批量生产工作台`。
- 单热点生成多篇的批次并发元数据已统一限制为 3。
- 安装卸载检查失败时不会继续交付。

## 安装卸载检查

```json
{
  "INNO_SETUP_INSTALL_PASS": true,
  "WINDOWS_APPS_ENTRY_PASS": true,
  "INNO_UNINSTALL_REAL_PASS": true,
  "INSTALL_DIR_REMOVED_PASS": true,
  "USER_DATA_PRESERVED_PASS": true,
  "install_returncode": 0,
  "uninstall_returncode": 0,
  "install_dir": "C:\\Users\\lenovo\\AppData\\Local\\Programs\\热点图文批量生产工作台",
  "data_dir": "C:\\Users\\lenovo\\AppData\\Local\\热点图文批量生产工作台"
}
```

## 测试记录

```json
{}
```

本报告不宣布客户交付通过。
