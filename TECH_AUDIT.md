# RC1.3.3-Lite-R2.2.7 技术审计摘要

本轮审计覆盖 1000 元客户版最终可交付收口。

关键实现：

- `modules/app_version.py` 当前版本为 RC1.3.3-Lite-R2.2.7。
- `scripts/build_rc1_3_3_lite_r2_2_7.py` 使用 Inno Setup 编译正式 Windows Setup。
- `packaging/inno_cleanup.ps1` 只按安装路径、命令行路径和本产品 runtime PID 清理进程，避免误杀其他 Python。
- `api.py` 恢复单热点多篇和多热点各一篇两种模式，总文章数最多 5。
- `ui/rc1_app.py` 恢复普通用户可见的创作模式选择和 1～5 篇数量选择。
- `research/service.py` 增加 60 秒总时限、8 个候选、8 秒网页抓取超时和可用来源达标即停。
- `generation/content_quality.py` 只把无依据硬事实作为硬错误；普通分析表达变为可核对提示。
- `generation/image_budget.py` 固定经济模式每篇 1 张封面，标准模式每篇 1 张封面＋1 张正文图。
- `generation/image_prompt_generator.py` 将文章标题和角度写入封面提示词，正文图使用不同构图。

保留边界：

- 授权算法未修改。
- 热点抓取架构未重做。
- 研究系统不继续扩展为专业新闻事实核验系统。
- 图片自动重试固定 0。
- Word 自动排版核心保留。
- 最后可信时间 `license_last_seen_utc` 使用 Windows DPAPI 保护。
- 状态 JSON 会保存回退状态、可信参考时间和恢复流程时间。
- 不提供防本地文件篡改、防反编译或专业级 DRM/反破解能力。

真实验收仍需用户执行：安装、Windows 已安装应用可见、卸载、文本文章能力测试、图片真实测试、1 篇纯文字、单热点 5 篇、多热点 5 篇、标准图片 2 张和 Word 导出。

完成状态：`RC1.3.3-Lite-R2.2.7 Codex自检完成，等待用户最终交付复测`
