# Material Parser

This directory contains material parsing logic, including raw material loading, cleanup, structured extraction, and source normalization.

## Generate Company Profile

Upload all supported documents from a directory to Doubao, ask Doubao to combine the uploaded materials with web search, and save a Markdown company profile for downstream article generation.

```powershell
cd <项目根目录>
python -m material_parser.company_profile `
  --input-dir "material_parser\data\小猫AI" `
  --company-name "小猫AI"
```

Optional output path:

```powershell
python -m material_parser.company_profile `
  --input-dir "material_parser\data\小猫AI" `
  --company-name "小猫AI" `
  --output "material_parser\outputs\小猫AI公司说明.md"
```

Supported document types: `pdf`, `doc`, `docx`, `txt`, `md`, `xlsx`, `xls`, `csv`, `ppt`, `pptx`.

## API

Start the material parser API:

```powershell
cd <项目根目录>
python -m uvicorn material_parser.api:app --host 127.0.0.1 --port 8020
```

Optional environment variables:

```powershell
$env:GEO_DOUBAO_CONFIG = "indexing_test/config.doubao.json"
$env:MATERIAL_PARSER_OUTPUT_DIR = "material_parser/outputs"
```

Create a company profile job:

```powershell
$body = @{
  input_dir = "material_parser/data/小猫AI"
  company_name = "小猫AI"
  config_path = "indexing_test/config.doubao.json"
  output_dir = "material_parser/outputs"
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8020/api/material-parser/company-profile/jobs" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

The create endpoint returns `job_id` and `markdown_path` immediately. Poll job status:

```powershell
Invoke-RestMethod "http://127.0.0.1:8020/api/material-parser/company-profile/jobs/<job_id>"
```

Response status values: `queued`, `running`, `succeeded`, `failed`.
