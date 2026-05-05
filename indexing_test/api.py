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
    pending_fields = collect_pending_report_fields(analysis)
    if pending_fields:
        set_stage(f"filling_audit_report_gaps_{len(pending_fields)}")
        try:
            gap_fill_response = send_prompt(
                page,
                config,
                build_gap_fill_prompt(
                    company_name=request.company_name,
                    test_environment=request.test_environment,
                    user_test_query=request.user_test_query,
                    rewritten_queries=rewritten_queries,
                    records=records,
                    citation_counts=citation_counts,
                    pending_fields=pending_fields,
                ),
                include_sources=False,
            )
            merge_gap_fill_response(analysis, gap_fill_response.text)
        except Exception as exc:
            print(f"Audit gap fill skipped: {exc}")

    output_path = output_path or make_output_path(request.output_dir, request.company_name)
    set_stage("writing_output_files")
    export_prompt_records_to_excel(
        records,
        output_path,
        summary={
            "report_title": str(analysis.get("report_title", REPORT_TITLE)),
            "test_environment": request.test_environment,
            "company_name": request.company_name,
            "user_test_query": request.user_test_query,
            "rewrite_count": str(request.rewrite_count),
            "analysis_conclusion": str(analysis.get("analysis_conclusion", "")),
            "report_sections": json.dumps(analysis.get("sections", {}), ensure_ascii=False),
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


# ---------------------------------------------------------------------------
# GEO audit report template v2
# ---------------------------------------------------------------------------


def U(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


REPORT_TITLE = U("GEO \\u4f18\\u5316\\u8bca\\u65ad\\u5ba1\\u8ba1\\u62a5\\u544a")
PENDING_TEXT = U("\\u6682\\u65e0\\u660e\\u786e\\u7ed3\\u8bba")

AUDIT_SECTION_SCHEMA: list[dict[str, Any]] = [
    {"key": 'basic_info', "title": U('\\u4e00\\u3001\\u62a5\\u544a\\u57fa\\u7840\\u4fe1\\u606f'), "fields": [('brand_name', U('\\u54c1\\u724c / \\u4f01\\u4e1a\\u540d\\u79f0')), ('diagnosis_date', U('\\u62a5\\u544a\\u8bca\\u65ad\\u65e5\\u671f')), ('main_business', U('\\u6838\\u5fc3\\u4e3b\\u8425\\u4ea7\\u54c1 / \\u4e1a\\u52a1')), ('competitors', U('\\u6838\\u5fc3\\u5bf9\\u6807\\u7ade\\u54c1')), ('geo_score', U('\\u5f53\\u524d GEO \\u6574\\u4f53\\u8bc4\\u5206\\uff0810 \\u5206\\u5236\\uff09')), ('summary_conclusion', U('\\u8bca\\u65ad\\u7ed3\\u8bba\\u603b\\u89c8')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'user_search_scenarios', "title": U('\\u4e8c\\u3001\\u54c1\\u724c\\u76ee\\u6807\\u7528\\u6237\\u753b\\u50cf & AI \\u641c\\u7d22\\u573a\\u666f\\u5206\\u6790'), "fields": [('target_users', U('\\u6838\\u5fc3\\u76ee\\u6807\\u4eba\\u7fa4')), ('search_scenarios', U('\\u9ad8\\u9891\\u641c\\u7d22\\u573a\\u666f')), ('common_question_patterns', U('\\u7528\\u6237\\u5e38\\u89c1\\u641c\\u7d22\\u63d0\\u95ee\\u53e5\\u5f0f')), ('unmet_search_needs', U('\\u6f5c\\u5728\\u672a\\u88ab\\u6ee1\\u8db3\\u7684\\u641c\\u7d22\\u9700\\u6c42')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'infrastructure_assessment', "title": U('\\u4e09\\u3001GEO \\u57fa\\u7840\\u57fa\\u5efa\\u73b0\\u72b6\\u8bc4\\u4f30'), "fields": [('official_channels', U('\\u5b98\\u65b9\\u9635\\u5730')), ('web_coverage', U('\\u5168\\u7f51\\u4fe1\\u606f\\u8986\\u76d6')), ('brand_entry_completeness', U('\\u54c1\\u724c\\u57fa\\u7840\\u8bcd\\u6761\\u5b8c\\u6574\\u6027')), ('existing_problems', U('\\u73b0\\u6709\\u57fa\\u5efa\\u5b58\\u5728\\u95ee\\u9898')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'authority_backlinks', "title": U('\\u56db\\u3001\\u6743\\u5a01\\u5a92\\u4f53 & \\u7b2c\\u4e09\\u65b9\\u80cc\\u4e66\\u6838\\u67e5'), "fields": [('media_reports', U('\\u5df2\\u6536\\u5f55\\u6743\\u5a01\\u5a92\\u4f53\\u62a5\\u9053\\u6e05\\u5355')), ('associations_certifications', U('\\u884c\\u4e1a\\u534f\\u4f1a / \\u8d44\\u8d28\\u8363\\u8a89 / \\u8ba4\\u8bc1\\u80cc\\u4e66\\u60c5\\u51b5')), ('source_quality_rating', U('\\u4fe1\\u6e90\\u8d28\\u91cf\\u8bc4\\u7ea7')), ('citation_grid_health', U('\\u5f15\\u7528\\u7f51\\u683c\\u5065\\u5eb7\\u5ea6')), ('missing_endorsements', U('\\u80cc\\u4e66\\u7f3a\\u5931\\u9879')), ('endorsement_suggestions', U('\\u80cc\\u4e66\\u8865\\u5145\\u5efa\\u8bae')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'ai_platform_mentions', "title": U('\\u4e94\\u3001\\u4e3b\\u6d41 AI \\u5e73\\u53f0\\u54c1\\u724c\\u63d0\\u53ca\\u7387\\u68c0\\u6d4b'), "fields": [('platforms_checked', U('\\u68c0\\u6d4b\\u5e73\\u53f0')), ('mention_matrix', U('\\u5404\\u5e73\\u53f0\\u54c1\\u724c\\u6709\\u65e0\\u63d0\\u53ca')), ('positive_mentions', U('\\u54c1\\u724c\\u6b63\\u9762\\u63d0\\u53ca\\u5185\\u5bb9\\u6982\\u62ec')), ('semantic_alignment', U('\\u8bed\\u4e49\\u5bf9\\u9f50\\u5ea6')), ('matched_semantic_tags', U('AI \\u8f93\\u51fa\\u5173\\u952e\\u8bcd\\u4e0e\\u54c1\\u724c\\u6838\\u5fc3\\u4f18\\u52bf\\u5339\\u914d\\u60c5\\u51b5')), ('sentiment_bias', U('\\u60c5\\u611f\\u504f\\u5411')), ('low_mention_reasons', U('\\u65e0\\u63d0\\u53ca / \\u63d0\\u53ca\\u8fc7\\u5c11\\u539f\\u56e0')), ('blank_positions', U('AI \\u5e73\\u53f0\\u7a7a\\u767d\\u70b9\\u4f4d\\u6c47\\u603b')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'search_heat_assessment', "title": U('\\u516d\\u3001\\u5168\\u7f51\\u641c\\u7d22\\u6307\\u6570 & \\u5e73\\u53f0\\u70ed\\u5ea6\\u8bc4\\u4f30'), "fields": [('search_heat_trend', U('\\u54c1\\u724c\\u5168\\u7f51\\u641c\\u7d22\\u70ed\\u5ea6\\u8d8b\\u52bf')), ('channel_mention_ratio', U('\\u5404\\u6e20\\u9053\\u54c1\\u724c\\u63d0\\u53ca\\u5360\\u6bd4')), ('high_traffic_gaps', U('\\u9ad8\\u6d41\\u91cf\\u6e20\\u9053\\u672a\\u5e03\\u5c40\\u70b9\\u4f4d')), ('search_weakness_summary', U('\\u641c\\u7d22\\u7aef\\u8584\\u5f31\\u73af\\u8282\\u603b\\u7ed3')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'competitor_benchmark', "title": U('\\u4e03\\u3001\\u7ade\\u54c1 GEO \\u5bf9\\u6807\\u5bf9\\u6bd4\\u5206\\u6790'), "fields": [('competitors', U('\\u7ade\\u54c1\\u540d\\u5355')), ('coverage_comparison', U('\\u7ade\\u54c1\\u5728 AI \\u5e73\\u53f0 / \\u641c\\u7d22\\u7aef\\u8986\\u76d6\\u60c5\\u51b5\\u5bf9\\u6bd4')), ('competitor_advantages', U('\\u7ade\\u54c1\\u4f18\\u52bf\\u70b9\\uff08\\u53ef\\u501f\\u9274\\uff09')), ('semantic_differentiation_comparison', U('\\u8bed\\u4e49\\u5dee\\u5f02\\u5316\\u5bf9\\u6bd4')), ('differentiation_breakthroughs', U('\\u6211\\u65b9\\u5dee\\u5f02\\u5316\\u7a81\\u7834\\u70b9')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'faq_opportunities', "title": U('\\u516b\\u3001\\u884c\\u4e1a & \\u7528\\u6237\\u9ad8\\u9891\\u95ee\\u9898\\u6c47\\u603b'), "fields": [('industry_common_faq', U('\\u884c\\u4e1a\\u901a\\u7528\\u9ad8\\u9891\\u95ee\\u7b54')), ('brand_specific_questions', U('\\u54c1\\u724c\\u4e13\\u5c5e\\u7528\\u6237\\u7591\\u95ee')), ('negative_sensitive_questions', U('\\u8d1f\\u9762\\u654f\\u611f\\u6f5c\\u5728\\u95ee\\u9898')), ('batch_qa_layout', U('\\u9700\\u6279\\u91cf\\u5e03\\u5c40\\u95ee\\u7b54\\u6e05\\u5355')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'sentiment_monitoring', "title": U('\\u4e5d\\u3001\\u54c1\\u724c\\u8206\\u60c5\\u76d1\\u6d4b\\u5206\\u6790'), "fields": [('positive_sentiment', U('\\u6b63\\u9762\\u8206\\u60c5\\u5185\\u5bb9')), ('neutral_sentiment', U('\\u4e2d\\u6027\\u8206\\u60c5\\u5185\\u5bb9')), ('negative_risks', U('\\u8d1f\\u9762\\u8206\\u60c5 / \\u98ce\\u9669\\u70b9')), ('risk_level_warning', U('\\u8206\\u60c5\\u98ce\\u9669\\u7b49\\u7ea7 & \\u9884\\u8b66\\u8bf4\\u660e')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'problem_summary', "title": U('\\u5341\\u3001GEO \\u95ee\\u9898\\u6c47\\u603b\\uff08\\u6838\\u5fc3\\u75db\\u70b9\\u6e05\\u5355\\uff09'), "fields": [('infrastructure_issues', U('\\u57fa\\u5efa\\u7c7b\\u95ee\\u9898')), ('ai_indexing_issues', U('AI \\u6536\\u5f55\\u7c7b\\u95ee\\u9898')), ('search_exposure_issues', U('\\u641c\\u7d22\\u66dd\\u5149\\u7c7b\\u95ee\\u9898')), ('trust_issues', U('\\u80cc\\u4e66\\u4fe1\\u4efb\\u7c7b\\u95ee\\u9898')), ('sentiment_risks', U('\\u8206\\u60c5\\u98ce\\u9669\\u7c7b\\u95ee\\u9898')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'execution_plan', "title": U('\\u5341\\u4e00\\u3001GEO \\u4f18\\u5316\\u843d\\u5730\\u6267\\u884c\\u65b9\\u6848'), "fields": [('phase_1_infrastructure', U('\\u7b2c\\u4e00\\u9636\\u6bb5\\uff1a\\u57fa\\u7840\\u57fa\\u5efa\\u8865\\u5168')), ('phase_2_authority_sources', U('\\u7b2c\\u4e8c\\u9636\\u6bb5\\uff1a\\u6743\\u5a01\\u80cc\\u4e66 & \\u4fe1\\u6e90\\u642d\\u5efa')), ('phase_3_ai_content_seeding', U('\\u7b2c\\u4e09\\u9636\\u6bb5\\uff1aAI \\u641c\\u7d22\\u5185\\u5bb9\\u9884\\u57cb')), ('phase_4_multi_platform_distribution', U('\\u7b2c\\u56db\\u9636\\u6bb5\\uff1a\\u5168\\u7f51\\u591a\\u5e73\\u53f0\\u94fa\\u91cf')), ('phase_5_monitoring_iteration', U('\\u7b2c\\u4e94\\u9636\\u6bb5\\uff1a\\u5b9a\\u671f\\u76d1\\u6d4b & \\u8fed\\u4ee3\\u4f18\\u5316')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
    {"key": 'expected_outcomes', "title": U('\\u5341\\u4e8c\\u3001\\u9884\\u671f\\u4f18\\u5316\\u6548\\u679c\\u76ee\\u6807'), "fields": [('day_30_goal', U('30 \\u5929\\u76ee\\u6807')), ('day_60_goal', U('60 \\u5929\\u76ee\\u6807')), ('day_90_goal', U('90 \\u5929\\u76ee\\u6807')), ('data_sources', U('\\u6570\\u636e\\u6765\\u6e90 / \\u5224\\u65ad\\u4f9d\\u636e'))]},
]


def is_blank_report_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped in {"?", "??", "???", "????", U("\\u5f85\\u6838\\u67e5"), PENDING_TEXT}
    return False


def build_analysis_prompt(company_name: str, test_environment: str, user_test_query: str, rewritten_queries: list[str], records: list[PromptRunRecord], citation_counts: list[dict[str, Any]]) -> str:
    result_summaries = [
        {
            "query": record.query,
            "result_excerpt": record.result[:900],
            "sources": [item for item in record.sources.splitlines() if item.strip()][:6],
            "source_urls": [item for item in record.source_urls.splitlines() if item.strip()][:6],
            "source_titles": [item for item in record.source_titles.splitlines() if item.strip()][:6],
        }
        for record in records
    ]
    schema = {section["key"]: {field_key: "" for field_key, _ in section["fields"]} for section in AUDIT_SECTION_SCHEMA}
    schema["basic_info"]["brand_name"] = company_name
    schema["basic_info"]["diagnosis_date"] = datetime.now().strftime("%Y-%m-%d")
    return f"""
{U('\\u4f60\\u662f\\u4e00\\u540d GEO \\u4f18\\u5316\\u8bca\\u65ad\\u5ba1\\u8ba1\\u987e\\u95ee\\u3002\\u8bf7\\u57fa\\u4e8e\\u6d4b\\u8bd5\\u6570\\u636e\\uff0c\\u751f\\u6210\\u7ed3\\u6784\\u5316 JSON\\uff0c\\u7528\\u4e8e\\u6e32\\u67d3\\u300aGEO \\u4f18\\u5316\\u8bca\\u65ad\\u5ba1\\u8ba1\\u62a5\\u544a\\u300b\\u3002')}

{U('\\u76ee\\u6807\\u54c1\\u724c / \\u4f01\\u4e1a')}: {company_name}
{U('\\u6d4b\\u8bd5\\u73af\\u5883')}: {test_environment}
{U('\\u539f\\u59cb\\u6d4b\\u8bd5\\u95ee\\u9898')}: {user_test_query}
{U('\\u6539\\u5199\\u6d4b\\u8bd5\\u95ee\\u9898')}: {json.dumps(rewritten_queries, ensure_ascii=False)}
AI {U('\\u5e73\\u53f0\\u6d4b\\u8bd5\\u7ed3\\u679c\\u6458\\u8981')}: {json.dumps(result_summaries, ensure_ascii=False)}
{U('\\u4fe1\\u6e90\\u5f15\\u7528\\u7edf\\u8ba1')}: {json.dumps(citation_counts, ensure_ascii=False)}

{U('\\u8f93\\u51fa\\u8981\\u6c42')}: 
1. {U('\\u53ea\\u8f93\\u51fa JSON \\u5bf9\\u8c61\\uff0c\\u4e0d\\u8981 Markdown\\uff0c\\u4e0d\\u8981\\u89e3\\u91ca\\u3002')}
2. {U('\\u5fc5\\u987b\\u5305\\u542b report_title\\u3001sections\\u3001analysis_conclusion \\u4e09\\u4e2a\\u9876\\u5c42\\u5b57\\u6bb5\\u3002')}
3. sections {U('\\u5fc5\\u987b\\u4e25\\u683c\\u4f7f\\u7528\\u4e0b\\u9762\\u7ed9\\u51fa\\u7684 12 \\u4e2a\\u952e\\uff0c\\u6bcf\\u4e2a\\u952e\\u4e0b\\u5fc5\\u987b\\u5305\\u542b\\u6307\\u5b9a\\u5b57\\u6bb5\\u3002')}
4. {U('\\u6bcf\\u4e2a\\u5b57\\u6bb5\\u4f18\\u5148\\u8f93\\u51fa 2-5 \\u6761\\u77ed\\u8981\\u70b9\\uff1b\\u6ca1\\u6709\\u4f9d\\u636e\\u65f6\\u5199\\u201c\\u6682\\u65e0\\u660e\\u786e\\u7ed3\\u8bba\\u201d\\uff0c\\u4e0d\\u8981\\u8f93\\u51fa\\u95ee\\u53f7\\u3002')}
5. {U('\\u4e0d\\u786e\\u5b9a\\u7684\\u4fe1\\u606f\\u4e0d\\u8981\\u7f16\\u9020\\u5177\\u4f53\\u8d44\\u8d28\\u3001\\u5a92\\u4f53\\u3001\\u641c\\u7d22\\u6307\\u6570\\u6216\\u7b2c\\u4e09\\u65b9\\u6570\\u636e\\u3002')}
6. {U('\\u5a92\\u4f53\\u3001\\u94fe\\u63a5\\u3001AI \\u63d0\\u53ca\\u60c5\\u51b5\\u53ea\\u80fd\\u57fa\\u4e8e\\u6d4b\\u8bd5\\u7ed3\\u679c\\u548c\\u4fe1\\u6e90\\u5f15\\u7528\\u7edf\\u8ba1\\u5f52\\u7eb3\\u3002')}
7. 每个章节的 data_sources 字段必须列出 2-5 条依据，例如：测试问题、AI 回答摘要、引用来源标题 / URL、平台检测结果、引用统计。
8. 第四章必须输出 source_quality_rating 和 citation_grid_health，评级可用“强 / 中 / 弱”，并说明引用网格覆盖官网、媒体、第三方平台、行业平台的情况。
9. 第五章必须输出 semantic_alignment、matched_semantic_tags、sentiment_bias，语义对齐度可用“高 / 中 / 低”，情感偏向可用“正向 / 中性 / 负向 / 模糊”。
10. 第七章必须输出 semantic_differentiation_comparison，对比我方与竞品在 AI 描述关键词、推荐理由、差异化卖点上的差异。

JSON {U('\\u7ed3\\u6784\\u6a21\\u677f')}: 
{json.dumps({"report_title": REPORT_TITLE, "analysis_conclusion": "", "sections": schema}, ensure_ascii=False, indent=2)}
""".strip()


def default_report_sections(company_name: str = "") -> dict[str, Any]:
    sections: dict[str, Any] = {}
    for section in AUDIT_SECTION_SCHEMA:
        sections[section["key"]] = {field_key: "" for field_key, _ in section["fields"]}
    sections["basic_info"].update({"brand_name": company_name, "diagnosis_date": datetime.now().strftime("%Y-%m-%d")})
    return sections


def normalize_report_sections(analysis: dict[str, Any], company_name: str = "") -> dict[str, Any]:
    source_sections = analysis.get("sections") if isinstance(analysis.get("sections"), dict) else {}
    sections = default_report_sections(company_name)
    for section in AUDIT_SECTION_SCHEMA:
        source = source_sections.get(section["key"], {}) if isinstance(source_sections, dict) else {}
        if not isinstance(source, dict):
            source = {}
        for field_key, _ in section["fields"]:
            value = source.get(field_key)
            if not is_blank_report_value(value):
                sections[section["key"]][field_key] = value
    if company_name and is_blank_report_value(sections["basic_info"].get("brand_name")):
        sections["basic_info"]["brand_name"] = company_name
    return sections


def parse_analysis(text: str) -> dict[str, Any]:
    try:
        payload = parse_json_payload(text)
        if isinstance(payload, dict):
            payload["report_title"] = REPORT_TITLE
            payload["analysis_conclusion"] = "" if is_blank_report_value(payload.get("analysis_conclusion")) else str(payload.get("analysis_conclusion"))
            payload["sections"] = normalize_report_sections(payload)
            return payload
    except ValueError:
        pass
    return {"report_title": REPORT_TITLE, "analysis_conclusion": text.strip(), "sections": default_report_sections()}




def collect_pending_report_fields(analysis: dict[str, Any], limit: int = 24) -> list[dict[str, str]]:
    sections = normalize_report_sections(analysis)
    pending: list[dict[str, str]] = []
    for section in AUDIT_SECTION_SCHEMA:
        section_data = sections.get(section["key"], {})
        for field_key, field_label in section["fields"]:
            if is_blank_report_value(section_data.get(field_key)):
                pending.append(
                    {
                        "section_key": section["key"],
                        "section_title": section["title"],
                        "field_key": field_key,
                        "field_label": field_label,
                    }
                )
                if len(pending) >= limit:
                    return pending
    return pending


def build_gap_fill_prompt(
    company_name: str,
    test_environment: str,
    user_test_query: str,
    rewritten_queries: list[str],
    records: list[PromptRunRecord],
    citation_counts: list[dict[str, Any]],
    pending_fields: list[dict[str, str]],
) -> str:
    result_summaries = [
        {
            "query": record.query,
            "result_excerpt": record.result[:1200],
            "sources": [item for item in record.sources.splitlines() if item.strip()][:8],
            "source_urls": [item for item in record.source_urls.splitlines() if item.strip()][:8],
            "source_titles": [item for item in record.source_titles.splitlines() if item.strip()][:8],
        }
        for record in records
    ]
    return f"""
{U('\\u4f60\\u6b63\\u5728\\u8865\\u5168\\u300aGEO \\u4f18\\u5316\\u8bca\\u65ad\\u5ba1\\u8ba1\\u62a5\\u544a\\u300b\\u4e2d\\u4ecd\\u4e3a\\u7a7a\\u6216\\u201c\\u6682\\u65e0\\u660e\\u786e\\u7ed3\\u8bba\\u201d\\u7684\\u5b57\\u6bb5\\u3002')}

{U('\\u76ee\\u6807\\u54c1\\u724c / \\u4f01\\u4e1a')}: {company_name}
{U('\\u6d4b\\u8bd5\\u73af\\u5883')}: {test_environment}
{U('\\u539f\\u59cb\\u6d4b\\u8bd5\\u95ee\\u9898')}: {user_test_query}
{U('\\u6539\\u5199\\u6d4b\\u8bd5\\u95ee\\u9898')}: {json.dumps(rewritten_queries, ensure_ascii=False)}
AI {U('\\u5e73\\u53f0\\u6d4b\\u8bd5\\u7ed3\\u679c\\u6458\\u8981')}: {json.dumps(result_summaries, ensure_ascii=False)}
{U('\\u4fe1\\u6e90\\u5f15\\u7528\\u7edf\\u8ba1')}: {json.dumps(citation_counts, ensure_ascii=False)}
{U('\\u9700\\u8981\\u8865\\u5168\\u7684\\u5b57\\u6bb5')}: {json.dumps(pending_fields, ensure_ascii=False)}

{U('\\u8f93\\u51fa\\u8981\\u6c42')}: 
1. {U('\\u53ea\\u8f93\\u51fa JSON \\u5bf9\\u8c61\\uff0c\\u4e0d\\u8981 Markdown\\uff0c\\u4e0d\\u8981\\u89e3\\u91ca\\u3002')}
2. {U('\\u53ea\\u8865\\u5168\\u201c\\u9700\\u8981\\u8865\\u5168\\u7684\\u5b57\\u6bb5\\u201d\\uff0c\\u4e0d\\u8981\\u8f93\\u51fa\\u5176\\u4ed6\\u5b57\\u6bb5\\u3002')}
3. {U('\\u9876\\u5c42\\u7ed3\\u6784\\u5fc5\\u987b\\u662f')}?{{"sections": {{"section_key": {{"field_key": value}}}}}}
4. {U('\\u6bcf\\u4e2a\\u5b57\\u6bb5\\u7ed9\\u51fa 2-4 \\u6761\\u77ed\\u8981\\u70b9\\uff0c\\u4f18\\u5148\\u57fa\\u4e8e\\u73b0\\u6709\\u6d4b\\u8bd5\\u7ed3\\u679c\\u5408\\u7406\\u5f52\\u7eb3\\u3002')}
5. {U('\\u5982\\u679c\\u6ca1\\u6709\\u76f4\\u63a5\\u8bc1\\u636e\\uff0c\\u53ef\\u4ee5\\u7ed9\\u51fa\\u201c\\u5efa\\u8bae\\u6838\\u67e5 / \\u5efa\\u8bae\\u8865\\u5145\\u201d\\u7684\\u884c\\u52a8\\u578b\\u7ed3\\u8bba\\uff0c\\u4f46\\u4e0d\\u8981\\u5199\\u201c\\u6682\\u65e0\\u660e\\u786e\\u7ed3\\u8bba\\u201d\\uff0c\\u4e5f\\u4e0d\\u8981\\u8f93\\u51fa\\u95ee\\u53f7\\u3002')}
6. {U('\\u4e0d\\u8981\\u7f16\\u9020\\u5177\\u4f53\\u5a92\\u4f53\\u6807\\u9898\\u3001\\u8bc1\\u4e66\\u3001\\u641c\\u7d22\\u6307\\u6570\\u3001\\u5e73\\u53f0\\u6570\\u636e\\uff1b\\u4e0d\\u786e\\u5b9a\\u65f6\\u5199\\u6210\\u5efa\\u8bae\\u6216\\u5f85\\u6838\\u67e5\\u65b9\\u5411\\u3002')}
7. {U('\\u5982\\u679c\\u8865\\u5168\\u5b57\\u6bb5\\u662f data_sources\\uff0c\\u5fc5\\u987b\\u5217\\u51fa\\u53ef\\u8ffd\\u6eaf\\u4f9d\\u636e\\uff0c\\u4f8b\\u5982\\u6d4b\\u8bd5\\u95ee\\u9898\\u3001AI \\u56de\\u7b54\\u6458\\u8981\\u3001\\u5f15\\u7528\\u6765\\u6e90\\u6807\\u9898 / URL\\u3001\\u5f15\\u7528\\u7edf\\u8ba1\\u3002')}
8. {U('\\u5982\\u679c\\u8865\\u5168\\u5b57\\u6bb5\\u6d89\\u53ca\\u8bed\\u4e49\\u5bf9\\u9f50\\u3001\\u60c5\\u611f\\u504f\\u5411\\u3001\\u4fe1\\u6e90\\u8d28\\u91cf\\u3001\\u5f15\\u7528\\u7f51\\u683c\\u3001\\u7ade\\u54c1\\u5dee\\u5f02\\u5316\\uff0c\\u5fc5\\u987b\\u7ed9\\u51fa\\u8bc4\\u7ea7 / \\u5224\\u65ad\\u548c\\u5bf9\\u5e94\\u4f9d\\u636e\\u3002')}
""".strip()


def merge_gap_fill_response(analysis: dict[str, Any], text: str) -> int:
    try:
        payload = parse_json_payload(text)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    incoming_sections = payload.get("sections")
    if not isinstance(incoming_sections, dict):
        return 0

    analysis_sections = analysis.setdefault("sections", default_report_sections())
    filled = 0
    for section in AUDIT_SECTION_SCHEMA:
        section_key = section["key"]
        incoming_section = incoming_sections.get(section_key)
        if not isinstance(incoming_section, dict):
            continue
        target_section = analysis_sections.setdefault(section_key, {})
        allowed_fields = {field_key for field_key, _ in section["fields"]}
        for field_key, value in incoming_section.items():
            if field_key not in allowed_fields or is_blank_report_value(value):
                continue
            if is_blank_report_value(target_section.get(field_key)):
                target_section[field_key] = value
                filled += 1
    analysis["sections"] = normalize_report_sections(analysis)
    return filled

def html_text(value: Any) -> str:
    if is_blank_report_value(value):
        return '<span class="empty-value">' + PENDING_TEXT + '</span>'
    if isinstance(value, list):
        items = [item for item in value if not is_blank_report_value(item)][:5]
        if not items:
            return '<span class="empty-value">' + PENDING_TEXT + '</span>'
        return '<ul class="bullet-list">' + ''.join(f'<li>{html_text(item)}</li>' for item in items) + '</ul>'
    if isinstance(value, dict):
        rows = []
        for key, item in list(value.items())[:6]:
            if is_blank_report_value(item):
                continue
            rows.append(f'<div class="kv-row"><span>{escape(str(key))}</span><strong>{html_text(item)}</strong></div>')
        return '<div class="kv-list">' + ''.join(rows) + '</div>' if rows else '<span class="empty-value">' + PENDING_TEXT + '</span>'
    return escape(str(value)).replace("\\n", "<br>")


def render_report_field(label: str, value: Any, index: int) -> str:
    compact_class = " is-empty" if is_blank_report_value(value) else ""
    return f'<article class="field-card{compact_class}"><h2><span>{index:02d}</span>{escape(label)}</h2><div class="field-value">{html_text(value)}</div></article>'


def render_report_page(section: dict[str, Any], section_data: dict[str, Any], page_number: int, total_pages: int) -> str:
    fields_html = ''.join(render_report_field(label, section_data.get(field_key), index) for index, (field_key, label) in enumerate(section["fields"], start=1))
    section_prefix, _, section_title = section["title"].partition(U("\\u3001"))
    section_title = section_title or section["title"]
    return f"""
    <section class="report-page">
      <div class="top-band"></div>
      <header class="page-header">
        <div class="title-block">
          <p class="eyebrow">GEO AUDIT REPORT</p>
          <div class="title-row"><span class="section-index">{escape(section_prefix)}</span><h1>{escape(section_title)}</h1></div>
        </div>
        <div class="page-number">{page_number:02d}<small>/{total_pages:02d}</small></div>
      </header>
      <div class="field-grid">{fields_html}</div>
      <footer class="page-footer"><span>{REPORT_TITLE}</span><span>AI {U('\\u641c\\u7d22\\u53ef\\u89c1\\u5ea6')} ? {U('\\u4fe1\\u6e90')} ? {U('\\u7ade\\u54c1')} ? {U('\\u8206\\u60c5')} ? {U('\\u6267\\u884c\\u65b9\\u6848')}</span></footer>
    </section>
"""


def build_html_report(payload: dict[str, Any]) -> str:
    analysis = payload.get("analysis", {}) if isinstance(payload.get("analysis"), dict) else {}
    company_name = str(payload.get("company_name") or "")
    sections = normalize_report_sections(analysis, company_name)
    pages = ''.join(render_report_page(section, sections.get(section["key"], {}), index, len(AUDIT_SECTION_SCHEMA)) for index, section in enumerate(AUDIT_SECTION_SCHEMA, start=1))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{REPORT_TITLE}</title>
  <style>
    @page {{ size: A4; margin: 0; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:#d9e2ef; color:#162033; font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif; }}
    .report-page {{ position:relative; width:210mm; height:297mm; margin:0 auto; padding:15mm 16mm 14mm; background:linear-gradient(180deg,#ffffff 0%,#f8fbff 100%); break-after:page; page-break-after:always; overflow:hidden; }}
    .report-page:last-child {{ break-after:auto; page-break-after:auto; }}
    .top-band {{ position:absolute; left:0; top:0; right:0; height:8mm; background:linear-gradient(90deg,#0f3b82,#1d75d8 52%,#2dd4bf); }}
    .page-header {{ position:relative; display:flex; justify-content:space-between; gap:12mm; padding-top:7mm; padding-bottom:6mm; border-bottom:1px solid #d6e2f2; }}
    .eyebrow {{ margin:0 0 3mm; color:#1d4ed8; font-size:9px; letter-spacing:2.2px; font-weight:800; }}
    .title-row {{ display:flex; align-items:flex-start; gap:4mm; }}
    .section-index {{ display:inline-flex; align-items:center; justify-content:center; min-width:17mm; height:12mm; padding:0 3mm; border-radius:999px; color:#fff; background:#1d4ed8; font-size:15px; font-weight:800; }}
    h1 {{ margin:0; color:#0f172a; font-size:24px; line-height:1.25; letter-spacing:-.2px; }}
    .page-number {{ color:#1d4ed8; font-size:24px; line-height:1; font-weight:900; }}
    .page-number small {{ color:#94a3b8; font-size:12px; font-weight:700; }}
    .field-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:4mm; margin-top:6mm; }}
    .field-card {{ min-height:23mm; padding:4mm; border:1px solid #d8e5f5; border-radius:14px; background:rgba(255,255,255,.88); box-shadow:0 6px 18px rgba(30,64,175,.06); overflow:hidden; }}
    .field-card.is-empty {{ min-height:16mm; background:#f4f7fb; border-style:dashed; }}
    .field-card:nth-child(1):last-child, .field-card:nth-last-child(1):nth-child(odd) {{ grid-column:span 2; }}
    .field-card h2 {{ display:flex; align-items:center; gap:2mm; margin:0 0 2.5mm; color:#0f3b82; font-size:13.5px; line-height:1.35; font-weight:800; }}
    .field-card h2 span {{ flex:0 0 auto; color:#38bdf8; font-size:11px; font-family:Arial,sans-serif; }}
    .field-value {{ color:#243044; font-size:12.1px; line-height:1.58; }}
    .bullet-list {{ margin:0; padding-left:1.15em; }}
    .bullet-list li {{ margin:0 0 1.2mm; }}
    .kv-list {{ display:grid; gap:1.8mm; }}
    .kv-row {{ display:grid; grid-template-columns:27% 1fr; gap:2mm; padding:1.5mm 0; border-bottom:1px dashed #dbe5f3; }}
    .kv-row span {{ color:#64748b; font-size:11px; }}
    .kv-row strong {{ color:#243044; font-weight:500; }}
    .empty-value {{ color:#94a3b8; font-style:normal; }}
    .page-footer {{ position:absolute; left:16mm; right:16mm; bottom:7mm; display:flex; justify-content:space-between; gap:6mm; padding-top:2.8mm; border-top:1px solid #d6e2f2; color:#64748b; font-size:10px; }}
    @media screen {{ .report-page {{ margin:14px auto; box-shadow:0 18px 48px rgba(15,23,42,.2); }} }}
    @media print {{ body {{ background:#fff; }} .report-page {{ margin:0; box-shadow:none; }} }}
  </style>
</head>
<body>
  <div style="display:none">{U('\\u54c1\\u724c')}: {escape(company_name)}; {U('\\u751f\\u6210\\u65f6\\u95f4')}: {escape(generated_at)}</div>
  {pages}
</body>
</html>"""


def write_html_output(output_path: Path, payload: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_html_report(payload), encoding="utf-8")
    return output_path


def write_pdf_output(html_path: Path, pdf_path: Path) -> Path:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError:
            browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        finally:
            browser.close()
    return pdf_path


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
