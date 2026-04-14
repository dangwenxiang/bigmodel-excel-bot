# BigModel Excel Bot

从 Excel 读取提示词，顺序发送到任意一个支持网页聊天的大模型页面，再把回答写回 Excel 的 `result` 列。

当前已经验证通过的 provider 是豆包，但项目本身不再绑定豆包，核心思路是：

1. `openpyxl` 读写 Excel
2. `Playwright` 打开浏览器并复用登录态
3. 通过配置里的选择器定位输入框、发送按钮、回答区域
4. 逐行发送 prompt
5. 等待回答稳定后回写到 Excel

## 适用场景

- 你手上有一份 Excel
- 其中一列是 prompt
- 需要串行处理，避免并发上下文串扰
- 想走网页自动化，而不是官方 API
- 不同平台只想改配置，不想改代码

## 目录

- [main.py](/Users/wenxiang/code/bigmodel-excel-bot/main.py): 主脚本
- [config.example.json](/Users/wenxiang/code/bigmodel-excel-bot/config.example.json): 通用配置模板
- [config.json](/Users/wenxiang/code/bigmodel-excel-bot/config.json): 当前可运行配置

## Excel 约定

默认读取表头：

- `prompt`
- `result`

例如：

| prompt | result |
| --- | --- |
| 帮我总结这段话 | |
| 给我一个标题 | |

## 使用步骤

1. 进入目录：

```bash
cd /Users/wenxiang/code/bigmodel-excel-bot
```

2. 安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

3. 准备配置：

```bash
cp config.example.json config.json
```

4. 修改 `config.json`

5. 运行：

```bash
python3 main.py --config config.json
```

## 常用参数

只跑前 5 行：

```bash
python3 main.py --config config.json --limit 5
```

覆盖已有结果重新跑：

```bash
python3 main.py --config config.json --overwrite
```

## 配置说明

### `excel`

- `path`: Excel 路径
- `sheet`: 工作表名，不填则用当前激活 sheet
- `header_row`: 表头所在行
- `prompt_column`: prompt 列名
- `result_column`: result 列名
- `start_row`: 起始处理行
- `skip_completed`: 已有结果是否跳过

### `browser`

- `start_url`: 打开的聊天页面地址
- `user_data_dir`: 浏览器用户目录，用来复用登录态
- `channel`: `chrome` 表示调用本机 Chrome
- `headless`: 是否无头模式
- `startup_wait_ms`: 打开页面后的初始等待时间
- `action_timeout_ms`: 浏览器动作超时

### `chat`

- `platform_name`: 平台名，只用于日志和提示
- `input_selectors`: 输入框候选选择器
- `send_button_selectors`: 发送按钮候选选择器
- `response_selectors`: 回答区域候选选择器
- `new_chat_selectors`: 新对话按钮候选选择器
- `loading_selectors`: 生成中状态候选选择器
- `response_timeout_seconds`: 单条回答等待超时
- `stability_checks`: 连续几轮文本不变后，判定回答结束
- `poll_interval_seconds`: 轮询间隔
- `send_hotkey`: 如果找不到发送按钮，退化为按键发送
- `clear_input_hotkey`: 清空输入框快捷键，macOS 通常是 `Meta+A`
- `new_chat_each_prompt`: 每条 prompt 前是否先点“新对话”
- `manual_login`: 未登录时是否等待你手动登录

## 豆包示例

当前 `config.json` 就是豆包配置，核心是：

- `browser.start_url`: `https://www.doubao.com/chat/`
- `chat.platform_name`: `doubao`
- `chat.response_selectors`: 包含 `.flow-markdown-body`

如果你后面要适配别的平台，比如 Kimi、DeepSeek、元宝、通义，通常只需要改：

- `start_url`
- `input_selectors`
- `send_button_selectors`
- `response_selectors`
- `new_chat_selectors`
- `loading_selectors`

## 兼容性

代码对旧格式做了兼容：

- 新格式推荐使用 `chat`
- 老的 `doubao` 配置段仍然能读

## 限制

1. 这是网页自动化，不是官方 API，页面结构一变就可能要调选择器。
2. 如果平台有验证码、风控、限流、强制弹窗，中途会影响自动化。
3. 不同平台的发送动作不完全一样，极端情况下需要再补平台特定逻辑。
