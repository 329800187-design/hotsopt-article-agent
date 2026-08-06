# Third-party notices

本项目没有复制第三方项目的源代码；以下依赖通过 `requirements.txt` 安装，许可证信息来自各项目公开仓库或发行包元数据。

| 组件 | 用途 | 许可证 | 项目地址 |
| --- | --- | --- | --- |
| FastAPI | 本地 API | MIT | https://github.com/fastapi/fastapi |
| Streamlit | 本地工作台 UI | Apache-2.0 | https://github.com/streamlit/streamlit |
| HTTPX | HTTP 客户端 | BSD-3-Clause | https://github.com/encode/httpx |
| Uvicorn | ASGI 服务启动 | BSD-3-Clause | https://github.com/encode/uvicorn |
| Pillow | 图片处理 | HPND | https://github.com/python-pillow/Pillow |
| python-docx | 后续 DOCX 导出框架 | MIT | https://github.com/python-openxml/python-docx |
| socksio | SOCKS 代理支持 | MIT | https://github.com/sethmlarson/socksio |

## 外部数据源

- 今日头条官方公开热榜页面：`https://www.toutiao.com/hot-event/hot-board/`。本项目只保存公开热点的规范化字段和来源链接，不复制其页面源代码。
- DailyHotApi：`https://github.com/imsyy/DailyHotApi`，作为可配置的备用聚合接口候选；本项目不内置其代码。
- NewsNow：`https://github.com/ourongxing/newsnow`，作为可配置的备用聚合来源参考；本项目不内置其代码。

使用外部热点数据时，交付方仍需遵守数据源、平台、版权和内容发布规则。第三方许可证变更时，应以其官方仓库和发行包中的最新文本为准。
