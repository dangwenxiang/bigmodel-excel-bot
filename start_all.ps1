param(
    [string]$Python = $(if ($env:PYTHON) { $env:PYTHON } else { "" }),
    [string]$HostName = $(if ($env:GEO_HOST) { $env:GEO_HOST } else { "127.0.0.1" }),
    [int]$IndexingPort = $(if ($env:GEO_INDEXING_PORT) { [int]$env:GEO_INDEXING_PORT } else { 8010 }),
    [int]$MaterialPort = $(if ($env:MATERIAL_PARSER_PORT) { [int]$env:MATERIAL_PARSER_PORT } else { 8020 }),
    [int]$ArticlePort = $(if ($env:ARTICLE_GENERATOR_PORT) { [int]$env:ARTICLE_GENERATOR_PORT } else { 8030 })
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $ProjectPython) {
        $Python = $ProjectPython
    } else {
        $Python = "python"
    }
}

function Start-ApiJob {
    param(
        [string]$Name,
        [string]$WorkingDirectory,
        [string]$App,
        [int]$Port
    )

    Write-Host "Starting ${Name}: http://${HostName}:${Port}"
    Start-Job -Name $Name -ArgumentList $Python, $WorkingDirectory, $App, $HostName, $Port -ScriptBlock {
        param($Python, $WorkingDirectory, $App, $HostName, $Port)
        Set-Location $WorkingDirectory
        & $Python -m uvicorn $App --host $HostName --port $Port 2>&1
    }
}

$jobs = @(
    Start-ApiJob -Name "geo-indexing" -WorkingDirectory (Join-Path $Root "indexing_test") -App "api:app" -Port $IndexingPort
    Start-ApiJob -Name "material-parser" -WorkingDirectory $Root -App "material_parser.api:app" -Port $MaterialPort
    Start-ApiJob -Name "article-generator" -WorkingDirectory $Root -App "article_generator.api:app" -Port $ArticlePort
)

Write-Host ""
Write-Host "All services are starting. Press Ctrl+C to stop."
Write-Host "Health checks:"
Write-Host "  收录测试:   http://${HostName}:${IndexingPort}/health"
Write-Host "  资料解析:   http://${HostName}:${MaterialPort}/health"
Write-Host "  文章生成:   http://${HostName}:${ArticlePort}/health"
Write-Host ""

try {
    while ($true) {
        foreach ($job in $jobs) {
            Receive-Job -Job $job -ErrorAction SilentlyContinue | ForEach-Object { "[$($job.Name)] $_" }
        }

        $failed = $jobs | Where-Object { $_.State -in @("Failed", "Stopped", "Completed") }
        if ($failed) {
            foreach ($job in $failed) {
                Write-Host "Service job exited: $($job.Name), state=$($job.State)"
                Receive-Job -Job $job -Keep | ForEach-Object { "[$($job.Name)] $_" }
            }
            throw "One or more services exited."
        }

        Start-Sleep -Seconds 2
    }
}
finally {
    Write-Host "Stopping services..."
    $jobs | Stop-Job -ErrorAction SilentlyContinue
    $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
}
