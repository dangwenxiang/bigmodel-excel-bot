#!/usr/bin/env python3

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from material_parser.company_profile import resolve_project_path, sanitize_filename

from .copywriter import DEFAULT_ARTICLE_OUTPUT_DIR, DEFAULT_CONFIG_PATH, generate_promotional_article


app = FastAPI(title="Article Generator API", version="1.0.0")
_JOB_LOCK = threading.Lock()
_JOB_STORE: dict[str, dict[str, Any]] = {}


class PromotionalArticleRequest(BaseModel):
    input_dir: str = Field(..., min_length=1, description="Directory containing Markdown and image files")
    topic: str = Field(..., min_length=1, description="Article topic or campaign theme")
    article_type: str = Field(default="品牌宣传文章", description="Article type")
    audience: str = Field(default="潜在客户", description="Target audience")
    tone: str = Field(default="专业、清晰、有转化力", description="Writing tone")
    config_path: str = Field(default=DEFAULT_CONFIG_PATH, description="Doubao runtime config path")
    output_path: str | None = Field(default=None, description="Markdown output file path")
    output_dir: str = Field(default=DEFAULT_ARTICLE_OUTPUT_DIR, description="Directory for generated Markdown")
    extra_instruction: str = Field(default="", description="Extra instruction appended to the generation prompt")
    interactive_login: bool = Field(default=False, description="Whether to pause for manual login")


def make_article_output_path(request: PromotionalArticleRequest) -> Path:
    if request.output_path:
        return resolve_project_path(request.output_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{sanitize_filename(request.topic)}-article.md"
    return resolve_project_path(request.output_dir) / filename


def set_job_state(job_key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        job = _JOB_STORE.setdefault(job_key, {})
        job.update(fields)


def run_article_job(job_id: str, request: PromotionalArticleRequest, article_path: Path) -> None:
    set_job_state(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        output_path, image_manifest_path = generate_promotional_article(
            input_dir=resolve_project_path(request.input_dir),
            topic=request.topic,
            article_type=request.article_type,
            audience=request.audience,
            tone=request.tone,
            config_path=resolve_project_path(request.config_path),
            output_path=article_path,
            extra_instruction=request.extra_instruction,
            interactive_login=request.interactive_login,
        )
        set_job_state(
            job_id,
            status="succeeded",
            finished_at=datetime.now().isoformat(),
            article_path=str(output_path),
            image_manifest_path=str(image_manifest_path),
            result={
                "article_path": str(output_path),
                "image_manifest_path": str(image_manifest_path),
                "presentation_type": "markdown_with_image_placeholders",
            },
            error=None,
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


def create_article_job(request: PromotionalArticleRequest) -> dict[str, Any]:
    input_dir = resolve_project_path(request.input_dir)
    if not input_dir.exists():
        raise HTTPException(status_code=400, detail=f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Input path is not a directory: {input_dir}")

    config_path = resolve_project_path(request.config_path)
    if not config_path.exists():
        raise HTTPException(status_code=400, detail=f"Config file not found: {config_path}")

    job_id = uuid.uuid4().hex
    created_at = datetime.now().isoformat()
    article_path = make_article_output_path(request)
    image_manifest_path = article_path.with_suffix(".images.json")
    set_job_state(
        job_id,
        job_id=job_id,
        status="queued",
        created_at=created_at,
        request=request.model_dump(),
        article_path=str(article_path),
        image_manifest_path=str(image_manifest_path),
        result=None,
        error=None,
    )
    worker = threading.Thread(target=run_article_job, args=(job_id, request, article_path), daemon=True)
    worker.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "created_at": created_at,
        "article_path": str(article_path),
        "image_manifest_path": str(image_manifest_path),
        "presentation_type": "markdown_with_image_placeholders",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/article-generator/promotional-articles/jobs")
def promotional_article_job(request: PromotionalArticleRequest) -> dict[str, Any]:
    return create_article_job(request)


@app.get("/api/article-generator/promotional-articles/jobs/{job_id}")
def promotional_article_job_status(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job
