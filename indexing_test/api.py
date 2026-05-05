#!/usr/bin/env python3

import json
import os
import re
import threading
import traceback
import uuid
from collections import Counter, defaultdict
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
DEFAULT_GEO_PLATFORM_CONFIGS: dict[str, str] = {
    "doubao": "config.doubao.json",
    "qwen": "config.qwen.json",
    "deepseek": "config.deepseek.json",
    "yuanbao": "config.yuanbao.json",
    "kimi": "config.kimi.json",
    "wenxin": "config.wenxin.json",
}
DEFAULT_GEO_TEST_PLATFORMS = ["doubao", "qwen", "deepseek", "yuanbao", "kimi", "wenxin"]
PLATFORM_DISPLAY_NAMES = {
    "doubao": "豆包",
    "qwen": "通义千问",
    "deepseek": "深度求索",
    "yuanbao": "腾讯元宝",
    "kimi": "月之暗面",
    "wenxin": "文心一言",
}
_JOB_LOCK = threading.Lock()
_GEO_RUN_LOCK = threading.Lock()
_JOB_STORE: dict[str, dict[str, Any]] = {}


class GeoOptimizeRequest(BaseModel):
    user_test_query: str = Field(..., min_length=1, description="User seed query")
    rewrite_count: int = Field(..., ge=1, le=50, description="Number of rewrites")
    company_name: str = Field(..., min_length=1, description="Company to optimize")
    test_environment: str = Field(default=DEFAULT_GEO_TEST_ENVIRONMENT, description="Test environment name")
    config_path: str = Field(default=DEFAULT_GEO_CONFIG_PATH, description="Runtime config path used for query rewrite and report generation")
    platform_config_paths: dict[str, str] | None = Field(default=None, description="Platform to config path mapping")
    test_platforms: list[str] | None = Field(default=None, description="Platforms to test with the same rewritten queries")
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


def repair_json_string_literals(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if not in_string:
            result.append(char)
            if char == '"':
                in_string = True
                escaped = False
            index += 1
            continue

        if escaped:
            result.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            index += 1
            continue
        if char == '"':
            cursor = index + 1
            while cursor < length and text[cursor].isspace():
                cursor += 1
            if cursor >= length or text[cursor] in ",:]}\n\r":
                result.append(char)
                in_string = False
            else:
                result.append('\\"')
            index += 1
            continue

        result.append(char)
        index += 1

    return "".join(result)


def normalize_model_json_text(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(
        U("\\u6539\\u5199\\u6d4b\\u8bd5\\u95ee\\u9898\\uff1a") + r'\["\s*([^"\]]+?)\s*",\s*"\s*([^"\]]+?)\s*"\]',
        lambda match: U("\\u6539\\u5199\\u6d4b\\u8bd5\\u95ee\\u9898\\uff1a") + f"[{match.group(1).strip()}；{match.group(2).strip()}]",
        normalized,
    )
    return normalized


def parse_json_payload_with_repair(text: str) -> Any:
    try:
        return parse_json_payload(text)
    except ValueError:
        repaired = repair_json_string_literals(normalize_model_json_text(text))
        try:
            return parse_json_payload(repaired)
        except ValueError:
            start = repaired.find("{")
            end = repaired.rfind("}")
            if start >= 0 and end > start:
                return json.loads(repaired[start : end + 1])
            raise


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


def resolve_platform_config_paths(request: GeoOptimizeRequest) -> list[tuple[str, Path]]:
    platform_config_paths = dict(DEFAULT_GEO_PLATFORM_CONFIGS)
    if request.platform_config_paths:
        platform_config_paths.update(request.platform_config_paths)

    platforms = request.test_platforms or DEFAULT_GEO_TEST_PLATFORMS
    resolved: list[tuple[str, Path]] = []
    missing: list[str] = []
    for platform in platforms:
        config_value = platform_config_paths.get(platform)
        if not config_value:
            missing.append(f"{platform}: no config path")
            continue
        config_path = resolve_api_path(config_value)
        if not config_path.exists():
            missing.append(f"{platform}: {config_path}")
            continue
        resolved.append((platform, config_path))

    if missing:
        raise HTTPException(
            status_code=400,
            detail="Missing platform config(s): " + "; ".join(missing),
        )
    if not resolved:
        raise HTTPException(status_code=400, detail="No platform configs available for testing.")
    return resolved


def make_platform_test_environment(base_environment: str, platform_names: list[str]) -> str:
    suffix = "+".join(platform_names)
    return f"{base_environment}:{suffix}" if suffix else base_environment

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


def make_job_log_path(output_path: Path, job_id: str | None = None) -> Path:
    logs_dir = output_path.parent / "logs"
    suffix = f"-{job_id[:8]}" if job_id else ""
    return logs_dir / f"{output_path.stem}{suffix}.log.jsonl"


def make_job_text_log_path(log_path: Path) -> Path:
    return log_path.with_suffix(".txt")


def append_job_log(log_path: Path | None, event: str, **fields: Any) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    with log_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def append_job_text_log(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    text_log_path = make_job_text_log_path(log_path)
    text_log_path.parent.mkdir(parents=True, exist_ok=True)
    with text_log_path.open("a", encoding="utf-8") as file:
        file.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")


def log_job_event(log_path: Path | None, event: str, **fields: Any) -> None:
    append_job_log(log_path, event, **fields)
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    append_job_text_log(log_path, f"{event}" + (f" {details}" if details else ""))


def build_file_path_payload(excel_path: Path) -> dict[str, str]:
    json_path = make_json_output_path(excel_path)
    html_path = make_html_output_path(excel_path)
    pdf_path = make_pdf_output_path(excel_path)
    log_path = make_job_log_path(excel_path)
    return {
        "excel_path": str(excel_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
        "log_path": str(log_path),
        "text_log_path": str(make_job_text_log_path(log_path)),
    }


def execute_geo_optimize(
    request: GeoOptimizeRequest,
    output_path: Path | None = None,
    stage_callback: Any | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    def set_stage(stage: str) -> None:
        log_job_event(log_path, "stage", stage=stage)
        if stage_callback:
            stage_callback(stage)

    config_path = resolve_api_path(request.config_path)
    if not config_path.exists():
        raise HTTPException(status_code=400, detail=f"Config file not found: {config_path}")

    config: AppConfig = load_config(config_path)
    platform_config_paths = resolve_platform_config_paths(request)
    platform_names = [platform for platform, _ in platform_config_paths]
    test_environment = make_platform_test_environment(request.test_environment, platform_names)
    log_job_event(
        log_path,
        "execute_start",
        company_name=request.company_name,
        rewrite_count=request.rewrite_count,
        config_path=str(config_path),
        output_path=str(output_path) if output_path else "",
        platforms=platform_names,
    )

    try:
        set_stage("opening_browser_for_rewrite")
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
            log_job_event(log_path, "rewrite_done", rewritten_count=len(rewritten_queries), rewritten_queries=rewritten_queries)

        records: list[PromptRunRecord] = []
        for platform_index, (platform, platform_config_path) in enumerate(platform_config_paths, start=1):
            platform_config = load_config(platform_config_path)
            set_stage(f"opening_{platform}_{platform_index}_of_{len(platform_config_paths)}")
            log_job_event(
                log_path,
                "platform_opening",
                platform=platform,
                platform_name=platform_display_name(platform),
                platform_index=platform_index,
                platform_count=len(platform_config_paths),
                config_path=str(platform_config_path),
            )
            with open_chat_page(platform_config, interactive_login=False) as (_, page):
                for query_index, query in enumerate(rewritten_queries, start=1):
                    set_stage(
                        f"testing_{platform}_query_{query_index}_of_{len(rewritten_queries)}"
                    )
                    log_job_event(
                        log_path,
                        "query_start",
                        platform=platform,
                        platform_name=platform_display_name(platform),
                        query_index=query_index,
                        query_count=len(rewritten_queries),
                        query=query,
                    )
                    print(f"Running {platform} query {query_index}/{len(rewritten_queries)}", flush=True)
                    response = send_prompt(page, platform_config, query)
                    print(f"Finished {platform} query {query_index}/{len(rewritten_queries)}", flush=True)
                    log_job_event(
                        log_path,
                        "query_done",
                        platform=platform,
                        platform_name=platform_display_name(platform),
                        query_index=query_index,
                        query_count=len(rewritten_queries),
                        result_length=len(response.text or ""),
                        source_count=len([item for item in response.sources.splitlines() if item.strip()]),
                        source_url_count=len([item for item in response.source_urls.splitlines() if item.strip()]),
                    )
                    records.append(
                        PromptRunRecord(
                            query=query,
                            result=response.text,
                            sources=response.sources,
                            source_urls=response.source_urls,
                            source_titles=response.source_titles,
                            platform=platform,
                        )
                    )

        citation_counts = build_reference_citation_counts(records)
        log_job_event(log_path, "platform_tests_done", record_count=len(records), citation_count=len(citation_counts))
        set_stage("building_final_audit_report")
        with open_chat_page(config, interactive_login=False) as (_, page):
            analysis_response = send_prompt(
                page,
                config,
                build_analysis_prompt(
                    company_name=request.company_name,
                    test_environment=test_environment,
                    user_test_query=request.user_test_query,
                    rewritten_queries=rewritten_queries,
                    records=records,
                    citation_counts=citation_counts,
                ),
                include_sources=False,
            )
        log_job_event(log_path, "analysis_response_done", response_length=len(analysis_response.text or ""))
    except HTTPException:
        log_job_event(log_path, "execute_http_exception", traceback=traceback.format_exc())
        raise
    except Exception as exc:
        log_job_event(log_path, "execute_exception", error=str(exc), traceback=traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    analysis = parse_analysis(analysis_response.text)
    inject_platform_result_overview(analysis, records, request.company_name)
    pending_fields = collect_pending_report_fields(analysis)
    log_job_event(log_path, "analysis_parsed", pending_field_count=len(pending_fields))
    if pending_fields:
        set_stage(f"filling_audit_report_gaps_{len(pending_fields)}")
        try:
            with open_chat_page(config, interactive_login=False) as (_, page):
                gap_fill_response = send_prompt(
                    page,
                    config,
                    build_gap_fill_prompt(
                        company_name=request.company_name,
                        test_environment=test_environment,
                        user_test_query=request.user_test_query,
                        rewritten_queries=rewritten_queries,
                        records=records,
                        citation_counts=citation_counts,
                        pending_fields=pending_fields,
                    ),
                    include_sources=False,
                )
            merge_gap_fill_response(analysis, gap_fill_response.text)
            inject_platform_result_overview(analysis, records, request.company_name)
            log_job_event(log_path, "gap_fill_done", response_length=len(gap_fill_response.text or ""))
        except Exception as exc:
            print(f"Audit gap fill skipped: {exc}")
            log_job_event(log_path, "gap_fill_skipped", error=str(exc), traceback=traceback.format_exc())

    output_path = output_path or make_output_path(request.output_dir, request.company_name)
    set_stage("writing_output_files")
    export_prompt_records_to_excel(
        records,
        output_path,
        summary={
            "report_title": str(analysis.get("report_title", REPORT_TITLE)),
            "test_environment": test_environment,
            "test_platforms": json.dumps(platform_names, ensure_ascii=False),
            "company_name": request.company_name,
            "user_test_query": request.user_test_query,
            "rewrite_count": str(request.rewrite_count),
            "analysis_conclusion": str(analysis.get("analysis_conclusion", "")),
            "report_sections": json.dumps(analysis.get("sections", {}), ensure_ascii=False),
        },
    )

    result = {
        "test_environment": test_environment,
        "test_platforms": platform_names,
        "company_name": request.company_name,
        "user_test_query": request.user_test_query,
        "rewritten_queries": rewritten_queries,
        "excel_path": str(output_path),
        "excel_rows": [
            {
                "platform": record.platform,
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
    log_job_event(log_path, "json_written", path=str(json_output_path), exists=json_output_path.exists(), size=json_output_path.stat().st_size if json_output_path.exists() else 0)
    write_html_output(html_output_path, result)
    log_job_event(log_path, "html_written", path=str(html_output_path), exists=html_output_path.exists(), size=html_output_path.stat().st_size if html_output_path.exists() else 0)
    set_stage("writing_pdf_file")
    write_pdf_output(html_output_path, pdf_output_path)
    log_job_event(log_path, "pdf_written", path=str(pdf_output_path), exists=pdf_output_path.exists(), size=pdf_output_path.stat().st_size if pdf_output_path.exists() else 0)
    log_job_event(log_path, "execute_done", excel_path=str(output_path), json_path=str(json_output_path), html_path=str(html_output_path), pdf_path=str(pdf_output_path))
    return result


def set_job_state(job_key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        job = _JOB_STORE.setdefault(job_key, {})
        job.update(fields)


def run_geo_job(job_id: str, request: GeoOptimizeRequest, output_path: Path) -> None:
    log_path = make_job_log_path(output_path, job_id)
    set_job_state(
        job_id,
        status="running",
        started_at=datetime.now().isoformat(),
        log_path=str(log_path),
        text_log_path=str(make_job_text_log_path(log_path)),
    )
    log_job_event(log_path, "job_started", job_id=job_id, output_path=str(output_path))
    try:
        set_job_state(job_id, stage="waiting_for_browser_slot")
        log_job_event(log_path, "stage", stage="waiting_for_browser_slot")
        with _GEO_RUN_LOCK:
            set_job_state(job_id, stage="running_browser_test")
            log_job_event(log_path, "stage", stage="running_browser_test")
            result = execute_geo_optimize(
                request,
                output_path=output_path,
                stage_callback=lambda stage: set_job_state(job_id, stage=stage),
                log_path=log_path,
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
        log_job_event(log_path, "job_succeeded", job_id=job_id)
    except HTTPException as exc:
        log_job_event(log_path, "job_failed", job_id=job_id, status_code=exc.status_code, detail=exc.detail)
        set_job_state(
            job_id,
            status="failed",
            stage="failed",
            finished_at=datetime.now().isoformat(),
            error={"status_code": exc.status_code, "detail": exc.detail},
        )
    except Exception as exc:
        log_job_event(log_path, "job_failed", job_id=job_id, error=str(exc), traceback=traceback.format_exc())
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
    log_path = make_job_log_path(output_path, job_id)
    file_paths["log_path"] = str(log_path)
    file_paths["text_log_path"] = str(make_job_text_log_path(log_path))
    log_job_event(log_path, "job_created", job_id=job_id, created_at=created_at, output_path=str(output_path), request=request.model_dump())
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

for _section in AUDIT_SECTION_SCHEMA:
    if _section["key"] == "ai_platform_mentions":
        _section["fields"].insert(
            3,
            (
                "platform_result_overview",
                U("\\u5404\\u5e73\\u53f0\\u6d4b\\u8bd5\\u7ed3\\u679c\\u6982\\u51b5"),
            ),
        )
        break


def is_blank_report_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped in {"?", "??", "???", "????", U("\\u5f85\\u6838\\u67e5"), PENDING_TEXT}
    if isinstance(value, list):
        return not value or all(is_blank_report_value(item) for item in value)
    if isinstance(value, dict):
        return not value or all(is_blank_report_value(item) for item in value.values())
    return False


def clip_text(value: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def format_percent(numerator: int, denominator: int) -> str:
    return "0%" if denominator <= 0 else f"{numerator / denominator * 100:.0f}%"


def platform_display_name(platform: str) -> str:
    return PLATFORM_DISPLAY_NAMES.get(platform, platform or U("\\u9ed8\\u8ba4\\u5e73\\u53f0"))


def localize_platform_names(value: str) -> str:
    text = str(value)
    for platform, display_name in PLATFORM_DISPLAY_NAMES.items():
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(platform)}(?![A-Za-z0-9_])", display_name, text)
    return text


def source_overview(record: PromptRunRecord, limit: int = 3) -> list[str]:
    titles = [item.strip() for item in record.source_titles.splitlines() if item.strip()]
    urls = [item.strip() for item in record.source_urls.splitlines() if item.strip()]
    sources = [item.strip() for item in record.sources.splitlines() if item.strip()]
    items: list[str] = []
    for index in range(max(len(titles), len(urls), len(sources))):
        title = titles[index] if index < len(titles) else ""
        url = urls[index] if index < len(urls) else ""
        source = sources[index] if index < len(sources) else ""
        label = title or source or url
        if not label:
            continue
        items.append(f"{clip_text(label, 36)}：{url}" if url and url not in label else clip_text(label, 60))
        if len(items) >= limit:
            break
    return items or [U("\\u672a\\u68c0\\u6d4b\\u5230\\u660e\\u786e\\u5f15\\u7528\\u4fe1\\u6e90")]


def build_platform_result_overview(records: list[PromptRunRecord], company_name: str) -> dict[str, Any]:
    grouped: dict[str, list[PromptRunRecord]] = defaultdict(list)
    for record in records:
        grouped[record.platform or U("\\u9ed8\\u8ba4\\u5e73\\u53f0")].append(record)

    overview: dict[str, Any] = {}
    for platform, platform_records in grouped.items():
        total_tests = len(platform_records)
        mention_hits = 0
        top_three_hits = 0
        first_hits = 0
        for record in platform_records:
            result = record.result or ""
            if not company_name or company_name not in result:
                continue
            mention_hits += 1
            position = result.find(company_name)
            prefix = result[:position]
            competitor_mentions_before = len(re.findall(U("\\u516c\\u53f8|\\u5382|\\u5382\\u5bb6|\\u6709\\u9650\\u516c\\u53f8"), prefix))
            if competitor_mentions_before == 0:
                first_hits += 1
            if competitor_mentions_before < 3:
                top_three_hits += 1

        overview[platform_display_name(platform)] = {
            U("\\u63d0\\u53ca\\u767e\\u5206\\u6bd4"): format_percent(mention_hits, total_tests),
            U("\\u524d\\u4e09\\u767e\\u5206\\u6bd4"): format_percent(top_three_hits, total_tests),
            U("\\u7b2c\\u4e00\\u767e\\u5206\\u6bd4"): format_percent(first_hits, total_tests),
        }
    return overview


def inject_platform_result_overview(analysis: dict[str, Any], records: list[PromptRunRecord], company_name: str) -> None:
    sections = analysis.setdefault("sections", default_report_sections(company_name))
    ai_section = sections.setdefault("ai_platform_mentions", {})
    ai_section.pop("platform_result_details", None)
    ai_section["platform_result_overview"] = build_platform_result_overview(records, company_name)


def build_analysis_prompt(company_name: str, test_environment: str, user_test_query: str, rewritten_queries: list[str], records: list[PromptRunRecord], citation_counts: list[dict[str, Any]]) -> str:
    result_summaries = [
        {
            "platform": platform_display_name(record.platform),
            "platform": platform_display_name(record.platform),
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
        payload = parse_json_payload_with_repair(text)
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
        payload = parse_json_payload_with_repair(text)
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
        for key, item in list(value.items())[:8]:
            if is_blank_report_value(item):
                continue
            row_class = "kv-row kv-row-block" if isinstance(item, dict) else "kv-row"
            rows.append(f'<div class="{row_class}"><span>{escape(localize_platform_names(str(key)))}</span><strong>{html_text(item)}</strong></div>')
        return '<div class="kv-list">' + ''.join(rows) + '</div>' if rows else '<span class="empty-value">' + PENDING_TEXT + '</span>'
    return escape(localize_platform_names(str(value))).replace("\\n", "<br>")


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
      <footer class="page-footer"><span>{REPORT_TITLE}</span><span>AI {U('\\u641c\\u7d22\\u53ef\\u89c1\\u5ea6')} · {U('\\u4fe1\\u6e90')} · {U('\\u7ade\\u54c1')} · {U('\\u8206\\u60c5')} · {U('\\u6267\\u884c\\u65b9\\u6848')}</span></footer>
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
    .kv-row-block {{ grid-template-columns:1fr; gap:1.2mm; padding:2mm; border:1px solid #dbeafe; border-radius:10px; background:#f8fbff; }}
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
    output_path = make_output_path(request.output_dir, request.company_name)
    log_path = make_job_log_path(output_path)
    log_job_event(log_path, "sync_request_started", output_path=str(output_path), request=request.model_dump())
    try:
        result = execute_geo_optimize(request, output_path=output_path, log_path=log_path)
        result["log_path"] = str(log_path)
        result["text_log_path"] = str(make_job_text_log_path(log_path))
        log_job_event(log_path, "sync_request_succeeded")
        return result
    except Exception as exc:
        log_job_event(log_path, "sync_request_failed", error=str(exc), traceback=traceback.format_exc())
        raise


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


@app.get("/api/geo-optimize/jobs/{job_id}/logs")
def geo_optimize_job_logs(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    log_path_value = job.get("log_path")
    if not log_path_value:
        return {"job_id": job_id, "entries": [], "text": "", "log_path": None, "text_log_path": None}

    log_path = Path(str(log_path_value))
    text_log_path = Path(str(job.get("text_log_path") or make_job_text_log_path(log_path)))
    entries: list[Any] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append({"raw": line})

    return {
        "job_id": job_id,
        "log_path": str(log_path),
        "text_log_path": str(text_log_path),
        "entries": entries,
        "text": text_log_path.read_text(encoding="utf-8") if text_log_path.exists() else "",
    }
