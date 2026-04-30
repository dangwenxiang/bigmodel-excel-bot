# Article Generator

This directory contains article generation logic, including outline, body, title, summary, and publish-ready draft generation from structured materials.

## Promotional Article Generator

Read all Markdown and image files from a directory, upload them to Doubao, and generate a Markdown promotional article. Images are represented by stable placeholders so a later renderer can replace them with real image components.

Placeholder format:

```text
{{IMAGE_SLOT:001|file=example.png|alt=图片说明|caption=图片标题}}
```

Final presentation recommendation:

- Use Markdown as the intermediate artifact for editing and review.
- Replace placeholders with images during rendering.
- Render target can be HTML, WeChat official account rich text, CMS JSON blocks, or any publishing-platform-specific format.

Run:

```powershell
cd <项目根目录>
python -m article_generator.copywriter `
  --input-dir "article_generator\data\campaign" `
  --topic "小猫AI企业宣传文章" `
  --article-type "公众号宣传文章" `
  --audience "中小企业老板和运营负责人"
```

Optional output path:

```powershell
python -m article_generator.copywriter `
  --input-dir "article_generator\data\campaign" `
  --topic "小猫AI企业宣传文章" `
  --output "article_generator\outputs\小猫AI宣传文章.md"
```

Supported input files:

- Markdown: `md`, `markdown`
- Images: `png`, `jpg`, `jpeg`, `webp`, `gif`, `bmp`

Outputs:

- `*.md`: generated promotional article with image placeholders.
- `*.images.json`: placeholder-to-image manifest for later replacement.

## API

Start the API:

```powershell
cd <项目根目录>
python -m uvicorn article_generator.api:app --host 127.0.0.1 --port 8030
```

Optional environment variables:

```powershell
$env:GEO_DOUBAO_CONFIG = "indexing_test/config.doubao.json"
$env:ARTICLE_GENERATOR_OUTPUT_DIR = "article_generator/outputs"
```

Create a promotional article job:

```powershell
$body = @{
  input_dir = "article_generator/data/campaign"
  topic = "小猫AI企业宣传文章"
  article_type = "公众号宣传文章"
  audience = "中小企业老板和运营负责人"
  tone = "专业、清晰、有转化力"
  output_dir = "article_generator/outputs"
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8030/api/article-generator/promotional-articles/jobs" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

Poll status:

```powershell
Invoke-RestMethod "http://127.0.0.1:8030/api/article-generator/promotional-articles/jobs/<job_id>"
```
