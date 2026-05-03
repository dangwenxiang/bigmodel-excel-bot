#!/usr/bin/env python3

import json
import os
import re
import threading
import traceback
import uuid
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from main import (
    AppConfig,
    PromptRunRecord,
    export_prompt_records_to_excel,
    load_config,
    open_chat_page,
    send_prompt,
)


app = FastAPI(title="GEO Test API", version="1.0.0")
API_DIR = Path(__file__).resolve().parent
DEFAULT_GEO_CONFIG_PATH = os.getenv("GEO_INDEXING_CONFIG", "config.doubao.json")
DEFAULT_GEO_OUTPUT_DIR = os.getenv("GEO_INDEXING_OUTPUT_DIR", "data/geo-runs")
DEFAULT_GEO_TEST_ENVIRONMENT = os.getenv("GEO_TEST_ENVIRONMENT", "geo")
_JOB_LOCK = threading.Lock()
_GEO_RUN_LOCK = threading.Lock()
_JOB_STORE: dict[str, dict[str, Any]] = {}


class GeoOptimizeRequest(BaseModel):
    user_test_query: str = Field(..., min_length=1, description="User seed query")
    rewrite_count: int = Field(..., ge=1, le=50, description="Number of rewrites")
    company_name: str = Field(..., min_length=1, description="Company to optimize")
    test_environment: str = Field(default=DEFAULT_GEO_TEST_ENVIRONMENT, description="Test environment name")
    config_path: str = Field(default=DEFAULT_GEO_CONFIG_PATH, description="Runtime config path")
    output_dir: str = Field(default=DEFAULT_GEO_OUTPUT_DIR, description="Directory for generated xlsx")


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower() or "geo-run"


def resolve_api_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = API_DIR / path
    return path.resolve()


def parse_json_payload(text: str) -> Any:
    raw = text.strip()
    candidates = [raw]

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(item.strip() for item in fenced if item.strip())

    for start_token, end_token in (("[", "]"), ("{", "}")):
        start = raw.find(start_token)
        end = raw.rfind(end_token)
        if start != -1 and end != -1 and end > start:
            candidates.append(raw[start : end + 1].strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("Model output is not valid JSON.")


def parse_rewrites(text: str, rewrite_count: int) -> list[str]:
    try:
        payload = parse_json_payload(text)
        if isinstance(payload, dict):
            payload = payload.get("queries") or payload.get("rewrites") or payload.get("items") or []
        if isinstance(payload, list):
            rewrites = [str(item).strip() for item in payload if str(item).strip()]
            if rewrites:
                return rewrites[:rewrite_count]
    except ValueError:
        pass

    rewrites: list[str] = []
    for line in text.splitlines():
        candidate = re.sub(r"^\s*\d+[\.\)\-、\s]+", "", line).strip(" -")
        if candidate:
            rewrites.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in rewrites:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    if not deduped:
        raise ValueError("Could not parse rewritten queries from model output.")
    return deduped[:rewrite_count]


def parse_analysis(text: str) -> dict[str, Any]:
    try:
        payload = parse_json_payload(text)
        if isinstance(payload, dict):
            return payload
    except ValueError:
        pass
    return {
        "report_title": "GEO 品牌 AI 审计报告",
        "analysis_conclusion": text.strip(),
        "final_ranking": [],
        "optimization_suggestions": [],
    }


def build_rewrite_prompt(user_test_query: str, rewrite_count: int, company_name: str) -> str:
    return f"""
你现在在做生成式引擎优化测试。

目标公司：{company_name}
原始测试话术：{user_test_query}

请把这句话改写成 {rewrite_count} 条语义相近、表达不同、适合搜索和问答测试的话术。

要求：
1. 保持核心意图一致
2. 每条都是一句完整中文
3. 不要重复
4. 不要解释
5. 只输出 JSON 数组
""".strip()


def build_analysis_prompt(
    company_name: str,
    test_environment: str,
    user_test_query: str,
    rewritten_queries: list[str],
    records: list[PromptRunRecord],
    citation_counts: list[dict[str, Any]],
) -> str:
    result_summaries = [
        {
            "query": record.query,
            "result_excerpt": record.result[:800],
            "sources": [item for item in record.sources.splitlines() if item.strip()],
            "source_urls": [item for item in record.source_urls.splitlines() if item.strip()],
            "source_titles": [item for item in record.source_titles.splitlines() if item.strip()],
        }
        for record in records
    ]
    return (
        "你是一名 GEO 品牌 AI 审计分析师。请基于以下多轮测试数据，输出《GEO 品牌 AI 审计报告》。\n"
        "报告必须围绕“可见、权重、信源、对比”四个维度展开，并给出可执行的 GEO 优化建议。\n\n"
        f"测试环境名：{test_environment}\n"
        f"目标公司：{company_name}\n"
        f"原始测试话术：{user_test_query}\n"
        f"改写测试话术：{json.dumps(rewritten_queries, ensure_ascii=False)}\n"
        f"测试结果：{json.dumps(result_summaries, ensure_ascii=False)}\n"
        f"参考资料引用次数：{json.dumps(citation_counts, ensure_ascii=False)}\n\n"
        "请只输出 JSON 对象，字段必须包含：\n"
        "{\n"
        '  "report_title": "GEO 品牌 AI 审计报告",\n'
        '  "test_environment": "geo",\n'
        '  "analysis_conclusion": "用一段话概括目标品牌当前 AI 可见度、推荐权重、信源可信度和竞品差距",\n'
        '  "audit_dimensions": {\n'
        '    "visibility_recall": {"dimension": "可见", "title": "基础可见度与唤醒率", "audit_points": "AI 是否能准确识别并描述目标品牌", "key_metrics": {"recall_rate": "N/总测试次数", "successful_recalls": 0, "total_tests": 0}, "findings": "现状判断", "risk_level": "高/中/低"},\n'
        '    "sov_first_mention": {"dimension": "权重", "title": "第一提及率与推荐占有率", "audit_points": "行业通用词下品牌在 AI 推荐列表中的排位和占有率", "key_metrics": {"first_mention_rate": "百分比", "sov": "百分比", "average_rank": "平均排名或未上榜"}, "findings": "现状判断", "risk_level": "高/中/低"},\n'
        '    "semantic_sentiment": {"dimension": "权重", "title": "语义对齐与情感偏向", "audit_points": "AI 输出关键词是否与品牌预设核心优势对齐，评价是否正向清晰", "key_metrics": {"semantic_tag_match": "高/中/低", "sentiment": "正向/中立/负向/模糊", "matched_tags": [], "missing_tags": []}, "findings": "现状判断", "risk_level": "高/中/低"},\n'
        '    "source_authority": {"dimension": "信源", "title": "信源质量与引用网格", "audit_points": "AI 主要引用官网、备案站点、权威媒体还是杂乱第三方平台", "key_metrics": {"authoritative_source_ratio": "百分比", "source_grid_health": "强/中/弱", "hallucination_risk": "高/中/低"}, "findings": "现状判断", "risk_level": "高/中/低"},\n'
        '    "competitor_benchmarking": {"dimension": "对比", "title": "竞品对比差异化", "audit_points": "竞品在同等语境下的 AI 推荐权重、语义簇覆盖和差异化优势", "key_metrics": {"leading_coefficient": "领先/持平/落后", "competitor_advantages": [], "untouched_semantic_clusters": []}, "findings": "现状判断", "risk_level": "高/中/低"}\n'
        '  },\n'
        '  "final_ranking": [{"rank": 1, "company": "公司名", "reason": "上榜原因", "mention_type": "第一提及/推荐提及/未提及"}],\n'
        '  "optimized_company_assessment": "目标公司当前数字化主权、AI 推荐权重和 GEO 基础设施判断",\n'
        '  "corpus_gap_scan": {"information_gaps": ["信息断层1"], "source_islands": ["信源孤岛1"], "hallucination_or_misreadings": ["误读或幻觉1"]},\n'
        '  "atomic_corpus_rebuild_suggestions": ["把散乱品牌介绍改造为 AI 易抓取的知识碎片建议1"],\n'
        '  "digital_sovereignty_assessment": {"official_source_status": "是否具备官网/备案/结构化数据", "icp_and_structured_data_risk": "高/中/低", "high_risk_items": []},\n'
        '  "visualization_summary": {"ai_gravity_score": 0, "radar_dimensions": {"visibility": 0, "recommendation_weight": 0, "authority": 0, "differentiation": 0}, "before_after_projection": {"current_first_mention_rate": "百分比", "expected_first_mention_rate_after_optimization": "百分比", "current_authority": "现状", "expected_authority_after_optimization": "预期"}},\n'
        '  "service_package_mapping": [{"detected_issue": "问题", "recommended_service": "对应服务套餐或动作", "reason": "推荐理由"}],\n'
        '  "optimization_suggestions": ["建议1", "建议2", "建议3"]\n'
        "}\n"
        "要求：\n"
        "1. 必须从“可见、权重、信源、对比”四个维度展开，不能只给泛泛结论\n"
        "2. 唤醒率按目标品牌在测试回答中被准确识别/描述的次数计算，无法精确时给出估算并说明依据\n"
        "3. SOV、第一提及率、平均排名要结合所有测试结果给出，不确定时说明样本限制\n"
        "4. 信源分析要区分官网/备案站点/权威媒体/第三方平台/无来源，并指出幻觉或误读风险\n"
        "5. 竞品对比要指出竞品占据的语义簇，以及目标品牌尚未触达的语义簇\n"
        "6. 原子化重构建议要具体到可生产的语料类型，例如官网 FAQ、业务卡片、案例页、口碑问答、对比页\n"
        "7. 结论部分要把发现的问题映射到后续服务动作，例如官网备案、GEO 结构化数据、权威信源铺设、语料重构\n"
        "8. 不要输出 JSON 以外的内容"
    )


def build_reference_citation_counts(records: list[PromptRunRecord]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for record in records:
        titles = [item.strip() for item in record.source_titles.splitlines() if item.strip()]
        urls = [item.strip() for item in record.source_urls.splitlines() if item.strip()]
        sources = [item.strip() for item in record.sources.splitlines() if item.strip()]

        row_items = titles or urls or sources
        for item in dict.fromkeys(row_items):
            counter[item] += 1

    return [
        {"reference": reference, "citation_count": count}
        for reference, count in counter.most_common()
    ]


def make_output_path(output_dir: str, company_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{sanitize_filename(company_name)}-geo.xlsx"
    return resolve_api_path(output_dir) / filename


def make_json_output_path(excel_path: Path) -> Path:
    return excel_path.with_suffix(".json")


def make_html_output_path(excel_path: Path) -> Path:
    return excel_path.with_suffix(".html")


def make_pdf_output_path(excel_path: Path) -> Path:
    return excel_path.with_suffix(".pdf")


def write_json_output(output_path: Path, payload: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def format_html_value(value: Any) -> str:
    if value is None or value == "":
        return "<span class=\"muted\">暂无</span>"
    if isinstance(value, (dict, list)):
        return f"<pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"
    return escape(str(value)).replace("\n", "<br>")


def render_metric_cards(analysis: dict[str, Any]) -> str:
    visualization = analysis.get("visualization_summary") if isinstance(analysis, dict) else {}
    radar = visualization.get("radar_dimensions", {}) if isinstance(visualization, dict) else {}
    cards = [
        ("AI 引力值", visualization.get("ai_gravity_score", "暂无") if isinstance(visualization, dict) else "暂无"),
        ("可见", radar.get("visibility", "暂无") if isinstance(radar, dict) else "暂无"),
        ("推荐权重", radar.get("recommendation_weight", "暂无") if isinstance(radar, dict) else "暂无"),
        ("权威度", radar.get("authority", "暂无") if isinstance(radar, dict) else "暂无"),
        ("差异化", radar.get("differentiation", "暂无") if isinstance(radar, dict) else "暂无"),
    ]
    return "".join(
        f"<div class=\"metric-card\"><div class=\"metric-label\">{escape(label)}</div>"
        f"<div class=\"metric-value\">{escape(str(value))}</div></div>"
        for label, value in cards
    )


def render_audit_dimensions(analysis: dict[str, Any]) -> str:
    dimensions = analysis.get("audit_dimensions", {}) if isinstance(analysis, dict) else {}
    if not isinstance(dimensions, dict) or not dimensions:
        return "<p class=\"muted\">暂无审计维度数据</p>"
    blocks: list[str] = []
    for key, item in dimensions.items():
        if not isinstance(item, dict):
            continue
        title = item.get("title") or key
        dimension = item.get("dimension") or ""
        risk_level = item.get("risk_level") or "暂无"
        blocks.append(
            "<article class=\"dimension-card\">"
            f"<div class=\"dimension-head\"><h3>{escape(str(title))}</h3>"
            f"<span class=\"tag\">{escape(str(dimension))}</span>"
            f"<span class=\"risk\">风险：{escape(str(risk_level))}</span></div>"
            f"<p><strong>审计要点：</strong>{format_html_value(item.get('audit_points'))}</p>"
            f"<p><strong>关键指标：</strong>{format_html_value(item.get('key_metrics'))}</p>"
            f"<p><strong>发现：</strong>{format_html_value(item.get('findings'))}</p>"
            "</article>"
        )
    return "".join(blocks) or "<p class=\"muted\">暂无审计维度数据</p>"


def render_ranking_rows(ranking: Any) -> str:
    if not isinstance(ranking, list) or not ranking:
        return "<tr><td colspan=\"4\" class=\"muted\">暂无排名数据</td></tr>"
    rows: list[str] = []
    for item in ranking:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{format_html_value(item.get('rank'))}</td>"
            f"<td>{format_html_value(item.get('company'))}</td>"
            f"<td>{format_html_value(item.get('mention_type'))}</td>"
            f"<td>{format_html_value(item.get('reason'))}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan=\"4\" class=\"muted\">暂无排名数据</td></tr>"


def render_excel_rows(rows: Any) -> str:
    if not isinstance(rows, list) or not rows:
        return "<tr><td colspan=\"5\" class=\"muted\">暂无测试明细</td></tr>"
    rendered: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        rendered.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{format_html_value(row.get('query'))}</td>"
            f"<td>{format_html_value(row.get('result'))}</td>"
            f"<td>{format_html_value(row.get('source_titles') or row.get('sources'))}</td>"
            f"<td>{format_html_value(row.get('source_urls'))}</td>"
            "</tr>"
        )
    return "".join(rendered) or "<tr><td colspan=\"5\" class=\"muted\">暂无测试明细</td></tr>"


def build_html_report(payload: dict[str, Any]) -> str:
    analysis = payload.get("analysis", {}) if isinstance(payload.get("analysis"), dict) else {}
    title = str(analysis.get("report_title") or "GEO 品牌 AI 审计报告")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f8fb; --card:#fff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; --brand:#1d4ed8; --soft:#eff6ff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif; line-height:1.65; }}
    .page {{ max-width:1180px; margin:0 auto; padding:32px 20px 56px; }}
    .hero {{ background:linear-gradient(135deg,#1d4ed8,#0f766e); color:#fff; border-radius:24px; padding:32px; box-shadow:0 18px 45px rgba(15,23,42,.16); }}
    .hero h1 {{ margin:0 0 12px; font-size:34px; letter-spacing:.02em; }}
    .hero p {{ margin:6px 0; opacity:.95; }}
    .section {{ margin-top:22px; background:var(--card); border:1px solid var(--line); border-radius:18px; padding:24px; box-shadow:0 8px 24px rgba(15,23,42,.05); }}
    .section h2 {{ margin:0 0 16px; font-size:22px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:14px; margin-top:18px; }}
    .metric-card {{ background:rgba(255,255,255,.15); border:1px solid rgba(255,255,255,.28); border-radius:16px; padding:16px; }}
    .metric-label {{ font-size:13px; opacity:.85; }}
    .metric-value {{ font-size:26px; font-weight:700; margin-top:4px; }}
    .dimension-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
    .dimension-card {{ border:1px solid var(--line); border-radius:16px; padding:18px; background:#fff; }}
    .dimension-head {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }}
    .dimension-head h3 {{ margin:0; font-size:18px; }}
    .tag,.risk {{ display:inline-flex; align-items:center; border-radius:999px; padding:3px 10px; font-size:12px; background:var(--soft); color:var(--brand); }}
    .risk {{ color:#b45309; background:#fffbeb; }}
    table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
    th,td {{ border:1px solid var(--line); padding:10px 12px; vertical-align:top; word-break:break-word; }}
    th {{ background:#f9fafb; text-align:left; }}
    pre {{ white-space:pre-wrap; word-break:break-word; margin:0; padding:12px; background:#f9fafb; border-radius:10px; border:1px solid var(--line); }}
    .muted {{ color:var(--muted); }}
    .two-col {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
    .info {{ background:#f9fafb; border:1px solid var(--line); border-radius:14px; padding:14px; }}
    @media (max-width: 900px) {{ .metrics,.dimension-grid,.two-col {{ grid-template-columns:1fr; }} .hero h1 {{ font-size:28px; }} }}
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <h1>{escape(title)}</h1>
      <p>品牌：{format_html_value(payload.get("company_name"))}</p>
      <p>测试环境：{format_html_value(payload.get("test_environment"))} ｜ 生成时间：{escape(generated_at)}</p>
      <div class="metrics">{render_metric_cards(analysis)}</div>
    </header>

    <section class="section">
      <h2>审计结论</h2>
      <p>{format_html_value(analysis.get("analysis_conclusion"))}</p>
      <div class="two-col">
        <div class="info"><strong>原始测试话术</strong><br>{format_html_value(payload.get("user_test_query"))}</div>
        <div class="info"><strong>改写测试话术</strong><br>{format_html_value(payload.get("rewritten_queries"))}</div>
      </div>
    </section>

    <section class="section">
      <h2>核心审计维度</h2>
      <div class="dimension-grid">{render_audit_dimensions(analysis)}</div>
    </section>

    <section class="section">
      <h2>推荐排名与竞品表现</h2>
      <table><thead><tr><th style="width:80px">排名</th><th>品牌</th><th>提及类型</th><th>原因</th></tr></thead><tbody>{render_ranking_rows(analysis.get("final_ranking"))}</tbody></table>
    </section>

    <section class="section">
      <h2>语料漏洞与数字化主权</h2>
      <div class="two-col">
        <div class="info"><strong>语料漏洞扫描</strong><br>{format_html_value(analysis.get("corpus_gap_scan"))}</div>
        <div class="info"><strong>数字化主权评估</strong><br>{format_html_value(analysis.get("digital_sovereignty_assessment"))}</div>
      </div>
    </section>

    <section class="section">
      <h2>优化建议</h2>
      <div class="two-col">
        <div class="info"><strong>原子化语料重构</strong><br>{format_html_value(analysis.get("atomic_corpus_rebuild_suggestions"))}</div>
        <div class="info"><strong>服务套餐映射</strong><br>{format_html_value(analysis.get("service_package_mapping"))}</div>
      </div>
      <div class="info" style="margin-top:16px"><strong>综合优化动作</strong><br>{format_html_value(analysis.get("optimization_suggestions"))}</div>
    </section>

    <section class="section">
      <h2>测试明细</h2>
      <table><thead><tr><th style="width:60px">#</th><th>测试问题</th><th>AI 回答</th><th>信源标题</th><th>信源链接</th></tr></thead><tbody>{render_excel_rows(payload.get("excel_rows"))}</tbody></table>
    </section>
  </main>
</body>
</html>
"""


def write_html_output(output_path: Path, payload: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_html_report(payload), encoding="utf-8")
    return output_path


def write_pdf_output(html_path: Path, pdf_path: Path) -> Path:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch_errors: list[Exception] = []
        browser = None
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError as exc:
            launch_errors.append(exc)
            browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={
                    "top": "16mm",
                    "right": "12mm",
                    "bottom": "16mm",
                    "left": "12mm",
                },
            )
        finally:
            browser.close()
    return pdf_path


def build_file_path_payload(excel_path: Path) -> dict[str, str]:
    json_path = make_json_output_path(excel_path)
    html_path = make_html_output_path(excel_path)
    pdf_path = make_pdf_output_path(excel_path)
    return {
        "excel_path": str(excel_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
    }


def execute_geo_optimize(
    request: GeoOptimizeRequest,
    output_path: Path | None = None,
    stage_callback: Any | None = None,
) -> dict[str, Any]:
    def set_stage(stage: str) -> None:
        if stage_callback:
            stage_callback(stage)

    config_path = resolve_api_path(request.config_path)
    if not config_path.exists():
        raise HTTPException(status_code=400, detail=f"Config file not found: {config_path}")

    config: AppConfig = load_config(config_path)

    try:
        set_stage("opening_browser")
        with open_chat_page(config, interactive_login=False) as (_, page):
            set_stage("rewriting_queries")
            rewrite_response = send_prompt(
                page,
                config,
                build_rewrite_prompt(
                    user_test_query=request.user_test_query,
                    rewrite_count=request.rewrite_count,
                    company_name=request.company_name,
                ),
            )
            rewritten_queries = parse_rewrites(rewrite_response.text, request.rewrite_count)

            records: list[PromptRunRecord] = []
            for index, query in enumerate(rewritten_queries, start=1):
                set_stage(f"testing_query_{index}_of_{len(rewritten_queries)}")
                response = send_prompt(page, config, query)
                records.append(
                    PromptRunRecord(
                        query=query,
                        result=response.text,
                        sources=response.sources,
                        source_urls=response.source_urls,
                        source_titles=response.source_titles,
                    )
                )

            citation_counts = build_reference_citation_counts(records)
            set_stage("building_final_audit_report")
            analysis_response = send_prompt(
                page,
                config,
                build_analysis_prompt(
                    company_name=request.company_name,
                    test_environment=request.test_environment,
                    user_test_query=request.user_test_query,
                    rewritten_queries=rewritten_queries,
                    records=records,
                    citation_counts=citation_counts,
                ),
                include_sources=False,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    analysis = parse_analysis(analysis_response.text)
    output_path = output_path or make_output_path(request.output_dir, request.company_name)
    set_stage("writing_output_files")
    export_prompt_records_to_excel(
        records,
        output_path,
        summary={
            "report_title": str(analysis.get("report_title", "GEO 品牌 AI 审计报告")),
            "test_environment": request.test_environment,
            "company_name": request.company_name,
            "user_test_query": request.user_test_query,
            "rewrite_count": str(request.rewrite_count),
            "analysis_conclusion": str(analysis.get("analysis_conclusion", "")),
            "optimized_company_assessment": str(analysis.get("optimized_company_assessment", "")),
            "audit_dimensions": json.dumps(analysis.get("audit_dimensions", {}), ensure_ascii=False),
            "corpus_gap_scan": json.dumps(analysis.get("corpus_gap_scan", {}), ensure_ascii=False),
            "digital_sovereignty_assessment": json.dumps(
                analysis.get("digital_sovereignty_assessment", {}),
                ensure_ascii=False,
            ),
            "visualization_summary": json.dumps(analysis.get("visualization_summary", {}), ensure_ascii=False),
            "service_package_mapping": json.dumps(analysis.get("service_package_mapping", []), ensure_ascii=False),
        },
    )

    result = {
        "test_environment": request.test_environment,
        "company_name": request.company_name,
        "user_test_query": request.user_test_query,
        "rewritten_queries": rewritten_queries,
        "excel_path": str(output_path),
        "excel_rows": [
            {
                "query": record.query,
                "result": record.result,
                "sources": record.sources,
                "source_urls": record.source_urls,
                "source_titles": record.source_titles,
            }
            for record in records
        ],
        "analysis": analysis,
        "reference_citation_counts": citation_counts,
    }
    json_output_path = make_json_output_path(output_path)
    html_output_path = make_html_output_path(output_path)
    pdf_output_path = make_pdf_output_path(output_path)
    result["json_path"] = str(json_output_path)
    result["html_path"] = str(html_output_path)
    result["pdf_path"] = str(pdf_output_path)
    write_json_output(json_output_path, result)
    write_html_output(html_output_path, result)
    set_stage("writing_pdf_file")
    write_pdf_output(html_output_path, pdf_output_path)
    return result


def set_job_state(job_key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        job = _JOB_STORE.setdefault(job_key, {})
        job.update(fields)


def run_geo_job(job_id: str, request: GeoOptimizeRequest, output_path: Path) -> None:
    set_job_state(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        set_job_state(job_id, stage="waiting_for_browser_slot")
        with _GEO_RUN_LOCK:
            set_job_state(job_id, stage="running_browser_test")
            result = execute_geo_optimize(
                request,
                output_path=output_path,
                stage_callback=lambda stage: set_job_state(job_id, stage=stage),
            )
        set_job_state(
            job_id,
            status="succeeded",
            stage="completed",
            finished_at=datetime.now().isoformat(),
            result=result,
            **build_file_path_payload(Path(result["excel_path"])),
            error=None,
        )
    except HTTPException as exc:
        set_job_state(
            job_id,
            status="failed",
            stage="failed",
            finished_at=datetime.now().isoformat(),
            error={"status_code": exc.status_code, "detail": exc.detail},
        )
    except Exception as exc:
        set_job_state(
            job_id,
            status="failed",
            stage="failed",
            finished_at=datetime.now().isoformat(),
            error={
                "status_code": 500,
                "detail": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def create_geo_job(request: GeoOptimizeRequest) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    created_at = datetime.now().isoformat()
    output_path = make_output_path(request.output_dir, request.company_name)
    file_paths = build_file_path_payload(output_path)
    set_job_state(
        job_id,
        job_id=job_id,
        status="queued",
        stage="queued",
        created_at=created_at,
        request=request.model_dump(),
        **file_paths,
        result=None,
        error=None,
    )
    worker = threading.Thread(target=run_geo_job, args=(job_id, request, output_path), daemon=True)
    worker.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "created_at": created_at,
        **file_paths,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/geo-optimize")
def geo_optimize(request: GeoOptimizeRequest) -> dict[str, Any]:
    return execute_geo_optimize(request)


@app.post("/api/geo-optimize/jobs")
def geo_optimize_job(request: GeoOptimizeRequest) -> dict[str, Any]:
    return create_geo_job(request)


@app.get("/api/geo-optimize/jobs/{job_id}")
def geo_optimize_job_status(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job
