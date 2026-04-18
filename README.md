# BigModel Excel Bot

把 Excel 里的 `prompt` 逐行发送到网页聊天模型，再把回答写回 Excel。

当前仓库保留的是一套最小可用核心：
- [main.py](/D:/projects/geo/bigmodel-excel-bot/main.py)
- [requirements.txt](/D:/projects/geo/bigmodel-excel-bot/requirements.txt)
- [config.example.json](/D:/projects/geo/bigmodel-excel-bot/config.example.json)
- [config.doubao.json](/D:/projects/geo/bigmodel-excel-bot/config.doubao.json)
- [config.json](/D:/projects/geo/bigmodel-excel-bot/config.json)
- [mock_chat.html](/D:/projects/geo/bigmodel-excel-bot/mock_chat.html)
- [data/prompts.xlsx](/D:/projects/geo/bigmodel-excel-bot/data/prompts.xlsx)

**功能**

- 从 Excel 读取 `prompt` 列
- 顺序发送到网页聊天页
- 把回答写回 `result` 列
- 如果配置了 `excel.source_column`，同步写入来源
- 对豆包优先提取结构化来源，必要时自动新增 `source_urls`、`source_titles`

**安装**

```bash
python -m pip install -r requirements.txt
```

如果本机还没装 Playwright 浏览器依赖，补一条：

```bash
python -m playwright install chromium
```

**快速开始**

本地模拟页面验证：

```bash
python main.py --config config.json --limit 2
```

豆包实跑：

```bash
python main.py --config config.doubao.json
```

首次跑豆包时会复用 [browser-profile-doubao](/D:/projects/geo/bigmodel-excel-bot/browser-profile-doubao) 里的登录态；如果未登录，脚本会等待你手动登录。

**Excel 格式**

最少需要两列：

| prompt | result |
| --- | --- |
| 帮我总结这段内容 | |
| 给我一个标题 | |

如果启用来源列，脚本会使用这些表头：

| prompt | result | sources | source_urls | source_titles |
| --- | --- | --- | --- | --- |

字段含义：

- `prompt`：输入给模型的问题
- `result`：模型回答
- `sources`：来源应用名或站点名
- `source_urls`：来源 URL，一行一个
- `source_titles`：来源标题，一行一个

**配置说明**

`excel`

- `path`：Excel 路径
- `sheet`：工作表名；不填则使用当前激活 sheet
- `header_row`：表头行号
- `prompt_column`：提示词列名
- `result_column`：结果列名
- `source_column`：来源列名；不填则不写来源
- `start_row`：起始处理行
- `skip_completed`：已有结果时是否跳过

`browser`

- `start_url`：聊天页面地址
- `user_data_dir`：浏览器用户目录，用于复用登录态
- `channel`：浏览器通道，当前主要用 `chrome`
- `headless`：是否无头运行
- `startup_wait_ms`：页面初始等待时间
- `action_timeout_ms`：单次浏览器操作超时

`chat`

- `platform_name`：平台名称，仅用于日志
- `input_selectors`：输入框候选选择器
- `send_button_selectors`：发送按钮候选选择器
- `response_selectors`：回答区域候选选择器
- `new_chat_selectors`：新对话按钮候选选择器
- `loading_selectors`：生成中状态候选选择器
- `popup_selectors`：弹窗候选选择器
- `popup_confirm_selectors`：弹窗确认按钮候选选择器
- `response_timeout_seconds`：单条回答等待上限
- `stability_checks`：回答稳定轮数
- `poll_interval_seconds`：轮询间隔
- `send_hotkey`：找不到发送按钮时使用的快捷键
- `clear_input_hotkey`：清空输入框快捷键
- `new_chat_each_prompt`：每条问题前是否先新建对话
- `manual_login`：未登录时是否等待手动登录
- `manual_popup_confirmation`：遇到风控/验证弹窗时是否等待人工处理

**来源提取**

通用兜底逻辑：

- 优先提取回答节点里的真实链接
- 如果没有链接，则尝试从回答正文里的“参考资料 / Sources / 参考来源”等段落提取

豆包增强逻辑：

- 从当前会话页的前端状态读取结构化来源
- 提取 `sitename / url / title`
- 分别写入 `sources / source_urls / source_titles`

之所以会额外打开当前 `/chat/<conversation_id>` 页面，是因为豆包主页面是 SPA，直接读取当前页状态容易混入历史会话；重新打开当前会话页后，结构化来源更稳定。

**常用命令**

只跑前 5 行：

```bash
python main.py --config config.doubao.json --limit 5
```

覆盖已有结果：

```bash
python main.py --config config.doubao.json --overwrite
```

**保留文件建议**

建议长期保留：

- 代码与配置文件
- [data/prompts.xlsx](/D:/projects/geo/bigmodel-excel-bot/data/prompts.xlsx)
- 浏览器登录目录 `browser-profile*`

可随时删除、需要时再生成：

- `__pycache__/`
- 调试截图、调试 HTML
- 临时验证 Excel
- Excel 锁文件 `~$*.xlsx`

**已知限制**

- 这是网页自动化，不是官方 API，页面结构变了就需要改选择器
- 豆包等平台如果触发验证码、风控或人工确认，脚本会暂停等待
- 来源质量受模型当前回答方式影响；如果平台本身没返回结构化来源，只能退回正文链接提取
