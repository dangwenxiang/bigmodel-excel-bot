#!/usr/bin/env python3

import json
import os
import re
import threading
import traceback
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
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
_JOB_LOCK = threading.Lock()
_JOB_STORE: dict[str, dict[str, Any]] = {}


class GeoOptimizeRequest(BaseModel):
    user_test_query: str = Field(..., min_length=1, description="User seed query")
    rewrite_count: int = Field(..., ge=1, le=50, description="Number of rewrites")
    company_name: str = Field(..., min_length=1, description="Company to optimize")
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
        "你是一名生成式引擎优化分析师。请基于以下测试数据，分析目标公司在生成式搜索/问答结果中的表现。\n\n"
        f"目标公司：{company_name}\n"
        f"原始测试话术：{user_test_query}\n"
        f"改写测试话术：{json.dumps(rewritten_queries, ensure_ascii=False)}\n"
        f"测试结果：{json.dumps(result_summaries, ensure_ascii=False)}\n"
        f"参考资料引用次数：{json.dumps(citation_counts, ensure_ascii=False)}\n\n"
        "请只输出 JSON 对象，字段必须包含：\n"
        "{\n"
        '  "analysis_conclusion": "整体结论",\n'
        '  "final_ranking": [{"rank": 1, "company": "公司名", "reason": "上榜原因"}],\n'
        '  "optimized_company_assessment": "目标公司当前表现判断",\n'
        '  "optimization_suggestions": ["建议1", "建议2", "建议3"]\n'
        "}\n"
        "要求：\n"
        "1. 排名要结合所有测试结果给出，不确定时说明依据\n"
        "2. 建议要围绕目标公司后续 GEO 优化动作\n"
        "3. 不要输出 JSON 以外的内容"
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


def write_json_output(output_path: Path, payload: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def build_file_path_payload(excel_path: Path) -> dict[str, str]:
    json_path = make_json_output_path(excel_path)
    return {
        "excel_path": str(excel_path),
        "json_path": str(json_path),
    }


def execute_geo_optimize(
    request: GeoOptimizeRequest,
    output_path: Path | None = None,
) -> dict[str, Any]:
    config_path = resolve_api_path(request.config_path)
    if not config_path.exists():
        raise HTTPException(status_code=400, detail=f"Config file not found: {config_path}")

    config: AppConfig = load_config(config_path)

    try:
        with open_chat_page(config, interactive_login=False) as (_, page):
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
            for query in rewritten_queries:
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
            analysis_response = send_prompt(
                page,
                config,
                build_analysis_prompt(
                    company_name=request.company_name,
                    user_test_query=request.user_test_query,
                    rewritten_queries=rewritten_queries,
                    records=records,
                    citation_counts=citation_counts,
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    analysis = parse_analysis(analysis_response.text)
    output_path = output_path or make_output_path(request.output_dir, request.company_name)
    export_prompt_records_to_excel(
        records,
        output_path,
        summary={
            "company_name": request.company_name,
            "user_test_query": request.user_test_query,
            "rewrite_count": str(request.rewrite_count),
            "analysis_conclusion": str(analysis.get("analysis_conclusion", "")),
            "optimized_company_assessment": str(analysis.get("optimized_company_assessment", "")),
        },
    )

    result = {
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
    result["json_path"] = str(json_output_path)
    write_json_output(json_output_path, result)
    return result


def set_job_state(job_key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        job = _JOB_STORE.setdefault(job_key, {})
        job.update(fields)


def run_geo_job(job_id: str, request: GeoOptimizeRequest, output_path: Path) -> None:
    set_job_state(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        result = execute_geo_optimize(request, output_path=output_path)
        set_job_state(
            job_id,
            status="succeeded",
            finished_at=datetime.now().isoformat(),
            result=result,
            **build_file_path_payload(Path(result["excel_path"])),
            error=None,
        )
    except HTTPException as exc:
        set_job_state(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error={"status_code": exc.status_code, "detail": exc.detail},
        )
    except Exception as exc:
        set_job_state(
            job_id,
            status="failed",
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
