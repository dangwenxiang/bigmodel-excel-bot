# GEO 项目接口说明

本文档说明当前项目的 3 个接口服务：收录测试、资料解析、文章生成。

以下命令中的 `$PROJECT_ROOT` 表示项目根目录。PowerShell 示例：

```powershell
$PROJECT_ROOT = "<你的项目根目录>"
$PYTHON = ".venv\Scripts\python.exe"
```

项目默认使用根目录下的 `.venv` 虚拟环境。若 `.venv` 不存在，启动脚本会回退到 `python`。也可以通过 `$PYTHON` 或 `PYTHON` 指定解释器路径。

## 使用 uv 创建 .venv 环境

推荐使用 uv 管理项目虚拟环境：

```powershell
cd $PROJECT_ROOT
uv venv .venv --python <Python解释器路径>
.\.venv\Scripts\python.exe -m pip install -r indexing_test\requirements.txt
```

如果本机已有可用的 Conda `GEO` 环境，也可以创建一个复用该环境依赖的 `.venv`：

```powershell
cd $PROJECT_ROOT
uv venv .venv --clear --system-site-packages --python <GEO环境python路径>
```

可选环境变量：

| 变量 | 默认值 | 备注 |
| --- | --- | --- |
| GEO_DOUBAO_CONFIG | `indexing_test/config.doubao.json` | 资料解析和文章生成共用的豆包配置路径 |
| MATERIAL_PARSER_OUTPUT_DIR | `material_parser/outputs` | 资料解析默认输出目录 |
| ARTICLE_GENERATOR_OUTPUT_DIR | `article_generator/outputs` | 文章生成默认输出目录 |
| GEO_INDEXING_CONFIG | `config.doubao.json` | 收录测试配置路径，相对 `indexing_test` 目录解析 |
| GEO_INDEXING_OUTPUT_DIR | `data/geo-runs` | 收录测试输出目录，相对 `indexing_test` 目录解析 |
| PYTHON | `.venv` 中的 Python，缺失时回退 `python` | 一键启动脚本使用的 Python 命令或解释器路径 |
| GEO_HOST | `127.0.0.1` | 一键启动脚本绑定地址 |
| GEO_INDEXING_PORT | `8010` | 收录测试服务端口 |
| MATERIAL_PARSER_PORT | `8020` | 资料解析服务端口 |
| ARTICLE_GENERATOR_PORT | `8030` | 文章生成服务端口 |

## 豆包固定 Chrome/CDP 运行

`indexing_test/config.doubao.json` 默认启用 CDP 连接、低频率随机节奏和人工接管验证。运行任务前先启动一个固定用户目录的 Chrome，并在里面登录豆包：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="D:\projects\geo\bigmodel-excel-bot\indexing_test\browser-profile-doubao"
```

如果 Chrome 安装路径不同，请替换为本机实际路径。触发豆包验证时，脚本会暂停等待你在该 Chrome 窗口中手动完成验证，然后自动继续。

## 一键启动全部服务

Windows PowerShell：

```powershell
cd $PROJECT_ROOT
.\start_all.ps1
```

指定 Python 解释器或端口：

```powershell
.\start_all.ps1 -Python "python" -HostName "127.0.0.1" -IndexingPort 8010 -MaterialPort 8020 -ArticlePort 8030
```

Linux/macOS/Git Bash：

```bash
cd "$PROJECT_ROOT"
bash ./start_all.sh
```

指定 Python 解释器或端口：

```bash
PYTHON=python GEO_HOST=127.0.0.1 GEO_INDEXING_PORT=8010 MATERIAL_PARSER_PORT=8020 ARTICLE_GENERATOR_PORT=8030 bash ./start_all.sh
```

启动后健康检查地址：

| 服务 | 地址 |
| --- | --- |
| 收录测试 | `http://127.0.0.1:8010/health` |
| 资料解析 | `http://127.0.0.1:8020/health` |
| 文章生成 | `http://127.0.0.1:8030/health` |

所有接口默认请求头一致：

## Header

| Key | Value | 备注 |
| --- | --- | --- |
| Content-Type | application/json | 请求体为 JSON |

---

# 1. 收录测试接口

用于生成测试话术、调用豆包搜索问答、提取来源并生成 Excel/JSON 结果文件。

## 启动方式

```powershell
cd $PROJECT_ROOT\indexing_test
& $PYTHON -m uvicorn api:app --host 127.0.0.1 --port 8010
```

健康检查：

```text
GET http://127.0.0.1:8010/health
```

## 创建任务

```text
POST http://127.0.0.1:8010/api/geo-optimize/jobs
```

## Body

| 字段 | 类型 | 必需 | 备注 |
| --- | --- | --- | --- |
| user_test_query | string | 是 | 原始测试话术，例如“榆林什么黄金珠宝店最好” |
| rewrite_count | integer | 是 | 改写话术数量，范围 1-50 |
| company_name | string | 是 | 目标公司名称 |
| config_path | string | 否 | 运行配置文件路径，默认读取 `GEO_INDEXING_CONFIG`，未设置时为 `config.doubao.json`，相对 `indexing_test` 目录解析 |
| output_dir | string | 否 | 输出目录，默认读取 `GEO_INDEXING_OUTPUT_DIR`，未设置时为 `data/geo-runs`，相对 `indexing_test` 目录解析 |

## 请求示例

```bash
curl -X POST "http://127.0.0.1:8010/api/geo-optimize/jobs" \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{
    "user_test_query": "榆林什么黄金珠宝店最好",
    "rewrite_count": 3,
    "company_name": "榆林周六福",
    "config_path": "config.doubao.json",
    "output_dir": "data/geo-runs"
  }'
```

## 创建任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态，创建后一般为 `queued` |
| created_at | string | 创建时间，ISO 格式 |
| excel_path | string | 预计生成的 Excel 文件路径 |
| json_path | string | 预计生成的 JSON 文件路径 |

## 查询任务状态

```text
GET http://127.0.0.1:8010/api/geo-optimize/jobs/{job_id}
```

## 查询任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态：`queued`、`running`、`succeeded`、`failed` |
| created_at | string | 创建时间 |
| started_at | string | 开始时间，任务运行后返回 |
| finished_at | string | 完成时间，任务结束后返回 |
| request | object | 创建任务时的请求体 |
| excel_path | string | Excel 结果文件路径 |
| json_path | string | JSON 结果文件路径 |
| result | object | 成功后返回完整结果；任务未完成时为 `null` |
| error | object | 失败时返回错误信息；成功或未失败时为 `null` |

## result 字段

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| company_name | string | 目标公司名称 |
| user_test_query | string | 原始测试话术 |
| rewritten_queries | string[] | 改写后的测试话术 |
| excel_path | string | Excel 结果文件路径 |
| json_path | string | JSON 结果文件路径 |
| excel_rows | object[] | 每条测试话术的问答结果 |
| analysis | object | GEO 分析结论 |
| reference_citation_counts | object[] | 来源引用统计 |

## excel_rows 字段

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| query | string | 实际测试话术 |
| result | string | 豆包回答内容 |
| sources | string | 来源站点，换行分隔 |
| source_urls | string | 来源 URL，换行分隔 |
| source_titles | string | 来源标题，换行分隔 |

---

# 2. 资料解析接口

用于读取指定目录下的公司资料文档，上传到豆包，并结合联网搜索生成 Markdown 格式公司说明。

支持文件类型：`pdf`、`doc`、`docx`、`txt`、`md`、`xlsx`、`xls`、`csv`、`ppt`、`pptx`。

## 启动方式

```powershell
cd $PROJECT_ROOT
& $PYTHON -m uvicorn material_parser.api:app --host 127.0.0.1 --port 8020
```

健康检查：

```text
GET http://127.0.0.1:8020/health
```

## 创建任务

```text
POST http://127.0.0.1:8020/api/material-parser/company-profile/jobs
```

## Body

| 字段 | 类型 | 必需 | 备注 |
| --- | --- | --- | --- |
| input_dir | string | 是 | 公司资料所在目录，支持相对项目根目录路径或绝对路径 |
| company_name | string | 否 | 公司名称；不传时使用 `input_dir` 的目录名 |
| config_path | string | 否 | 豆包运行配置路径，默认读取 `GEO_DOUBAO_CONFIG`，未设置时为 `indexing_test/config.doubao.json` |
| output_path | string | 否 | 指定 Markdown 输出文件路径；优先级高于 `output_dir` |
| output_dir | string | 否 | Markdown 输出目录，默认读取 `MATERIAL_PARSER_OUTPUT_DIR`，未设置时为 `material_parser/outputs` |
| extra_instruction | string | 否 | 追加给豆包的补充要求 |
| interactive_login | boolean | 否 | 是否允许未登录时在控制台等待手动登录；接口服务建议为 `false` |

## 请求示例

```bash
curl -X POST "http://127.0.0.1:8020/api/material-parser/company-profile/jobs" \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{
    "input_dir": "material_parser/data/小猫AI",
    "company_name": "小猫AI",
    "config_path": "indexing_test/config.doubao.json",
    "output_dir": "material_parser/outputs",
    "extra_instruction": "",
    "interactive_login": false
  }'
```

## 创建任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态，创建后一般为 `queued` |
| created_at | string | 创建时间，ISO 格式 |
| markdown_path | string | 预计生成的公司说明 Markdown 文件路径 |

## 查询任务状态

```text
GET http://127.0.0.1:8020/api/material-parser/company-profile/jobs/{job_id}
```

## 查询任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态：`queued`、`running`、`succeeded`、`failed` |
| created_at | string | 创建时间 |
| started_at | string | 开始时间，任务运行后返回 |
| finished_at | string | 完成时间，任务结束后返回 |
| request | object | 创建任务时的请求体 |
| markdown_path | string | 公司说明 Markdown 文件路径 |
| result | object | 成功后返回结果；任务未完成时为 `null` |
| error | object | 失败时返回错误信息；成功或未失败时为 `null` |

## result 字段

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| markdown_path | string | 生成的公司说明 Markdown 文件路径 |

---

# 3. 文章生成接口

用于读取指定目录下的 Markdown 文件和图片，上传到豆包，生成带图片占位符的宣传文章。

支持文件类型：

| 类型 | 后缀 |
| --- | --- |
| Markdown | `md`、`markdown` |
| 图片 | `png`、`jpg`、`jpeg`、`webp`、`gif`、`bmp` |

最终呈现方式：`Markdown + 图片占位符 + 图片清单 JSON`。

图片占位符格式：

```text
{{IMAGE_SLOT:001|file=example.png|alt=图片说明|caption=图片标题}}
```

后续渲染逻辑可以根据 `*.images.json` 将占位符替换为 HTML、公众号富文本、CMS 图片组件或其他发布平台格式。

## 启动方式

```powershell
cd $PROJECT_ROOT
& $PYTHON -m uvicorn article_generator.api:app --host 127.0.0.1 --port 8030
```

健康检查：

```text
GET http://127.0.0.1:8030/health
```

## 创建任务

```text
POST http://127.0.0.1:8030/api/article-generator/promotional-articles/jobs
```

## Body

| 字段 | 类型 | 必需 | 备注 |
| --- | --- | --- | --- |
| input_dir | string | 是 | Markdown 和图片所在目录，支持相对项目根目录路径或绝对路径 |
| topic | string | 是 | 文章主题或营销主题 |
| article_type | string | 否 | 文章类型，默认 `品牌宣传文章`，例如 `公众号宣传文章`、`SEO软文` |
| audience | string | 否 | 目标读者，默认 `潜在客户` |
| tone | string | 否 | 文案语气，默认 `专业、清晰、有转化力` |
| config_path | string | 否 | 豆包运行配置路径，默认读取 `GEO_DOUBAO_CONFIG`，未设置时为 `indexing_test/config.doubao.json` |
| output_path | string | 否 | 指定文章 Markdown 输出文件路径；优先级高于 `output_dir` |
| output_dir | string | 否 | 输出目录，默认读取 `ARTICLE_GENERATOR_OUTPUT_DIR`，未设置时为 `article_generator/outputs` |
| extra_instruction | string | 否 | 追加给豆包的补充要求 |
| interactive_login | boolean | 否 | 是否允许未登录时在控制台等待手动登录；接口服务建议为 `false` |

## 请求示例

```bash
curl -X POST "http://127.0.0.1:8030/api/article-generator/promotional-articles/jobs" \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary '{
    "input_dir": "article_generator/data/campaign",
    "topic": "小猫AI企业宣传文章",
    "article_type": "公众号宣传文章",
    "audience": "中小企业老板和运营负责人",
    "tone": "专业、清晰、有转化力",
    "config_path": "indexing_test/config.doubao.json",
    "output_dir": "article_generator/outputs",
    "extra_instruction": "",
    "interactive_login": false
  }'
```

## 创建任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态，创建后一般为 `queued` |
| created_at | string | 创建时间，ISO 格式 |
| article_path | string | 预计生成的宣传文章 Markdown 文件路径 |
| image_manifest_path | string | 预计生成的图片占位符清单 JSON 文件路径 |
| presentation_type | string | 最终呈现类型，当前为 `markdown_with_image_placeholders` |

## 查询任务状态

```text
GET http://127.0.0.1:8030/api/article-generator/promotional-articles/jobs/{job_id}
```

## 查询任务响应参数

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| job_id | string | 任务 ID |
| status | string | 任务状态：`queued`、`running`、`succeeded`、`failed` |
| created_at | string | 创建时间 |
| started_at | string | 开始时间，任务运行后返回 |
| finished_at | string | 完成时间，任务结束后返回 |
| request | object | 创建任务时的请求体 |
| article_path | string | 宣传文章 Markdown 文件路径 |
| image_manifest_path | string | 图片占位符清单 JSON 文件路径 |
| result | object | 成功后返回结果；任务未完成时为 `null` |
| error | object | 失败时返回错误信息；成功或未失败时为 `null` |

## result 字段

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| article_path | string | 生成的宣传文章 Markdown 文件路径 |
| image_manifest_path | string | 生成的图片占位符清单 JSON 文件路径 |
| presentation_type | string | 最终呈现类型，当前为 `markdown_with_image_placeholders` |

---

# 通用错误响应说明

任务执行失败时，状态查询接口的 `error` 字段会返回：

| 字段 | 类型 | 备注 |
| --- | --- | --- |
| status_code | integer | 错误码 |
| detail | string | 错误详情 |
| traceback | string | Python 堆栈，仅调试使用 |

常见失败原因：

| 原因 | 说明 |
| --- | --- |
| 登录态失效 | 豆包登录态不可用，需先用配置的 `browser-profile-doubao` 完成登录 |
| 输入目录不存在 | `input_dir` 路径错误 |
| 文件类型不支持 | 目录下没有接口支持的文件类型 |
| 上传控件变化 | 豆包网页结构变化导致无法自动上传文件 |
| 响应超时 | 豆包生成时间超过配置的 `response_timeout_seconds` |


| reference_citation_counts | object[] | ?????? |

## ??????

?????????? `*.json`?`*.html`?`*.pdf` ??????? AI ???????? 12 ???????????? HTML/PDF ????????? `analysis.sections` ???????

12 ??????????????????? AI ?????GEO ??????????AI ??????????????????????????????????????30/60/90 ????
