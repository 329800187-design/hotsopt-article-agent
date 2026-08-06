# L1 离线许可证签发工具

这是开发者专用的离线签发工具，不是客户运行包。

## 使用

1. 确认项目外存在 `%USERPROFILE%\hotspot-license-admin\license_private_key.pem`，或设置 `HOTSPOT_LICENSE_PRIVATE_KEY`。
2. 确认该私钥派生出的公钥与 `resources/license_public_key.pem` 一致。
3. 双击根目录的 `start-license-generator.bat`，或运行 `python -m license_admin.license_generator_gui`。
4. 输入客户名称、设备申请码和有效期，保存 `.license` 文件。

工具每次签发前都会比较私钥派生公钥和客户端公钥；不匹配时拒绝签发。私钥缺失时请恢复开发者备份，不要重新生成密钥，否则已交付客户端无法验证新许可证。

签名身份已经在客户端打包前完成初始化。最终签发工具不包含初始化命令，避免误生成新密钥。私钥丢失时必须恢复开发者备份。

本工具包不包含真实私钥，也不提供联网激活、服务器心跳或支付功能。
