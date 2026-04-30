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

from .company_profile import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MATERIAL_OUTPUT_DIR,
    generate_company_profile,
    resolve_project_path,
    sanitize_filename,
)


app = FastAPI(title="Material Parser API", version="1.0.0")
_JOB_LOCK = threading.Lock()
_JOB_STORE: dict[str, dict[str, Any]] = {}


class CompanyProfileRequest(BaseModel):
    input_dir: str = Field(..., min_length=1, description="Directory containing company material documents")
    company_name: str | None = Field(default=None, description="Company name. Defaults to input directory name")
    config_path: str = Field(
        default=DEFAULT_CONFIG_PATH,
        description="Doubao runtime config path",
    )
    output_path: str | None = Field(default=None, description="Markdown output file path")
    output_dir: str = Field(
        default=DEFAULT_MATERIAL_OUTPUT_DIR,
        description="Directory for generated Markdown when output_path is omitted",
    )
    extra_instruction: str = Field(default="", description="Extra instruction appended to the analysis prompt")
    interactive_login: bool = Field(
        default=False,
        description="Whether to pause for manual login when the configured browser profile is not logged in",
    )


def make_markdown_output_path(request: CompanyProfileRequest) -> Path:
    if request.output_path:
        return resolve_project_path(request.output_path)

    input_dir = resolve_project_path(request.input_dir)
    company = request.company_name or input_dir.name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{sanitize_filename(company)}-company-profile.md"
    return resolve_project_path(request.output_dir) / filename


def set_job_state(job_key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        job = _JOB_STORE.setdefault(job_key, {})
        job.update(fields)


def run_company_profile_job(job_id: str, request: CompanyProfileRequest, markdown_path: Path) -> None:
    set_job_state(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        output_path = generate_company_profile(
            input_dir=resolve_project_path(request.input_dir),
            company_name=request.company_name,
            config_path=resolve_project_path(request.config_path),
            output_path=markdown_path,
            extra_instruction=request.extra_instruction,
            interactive_login=request.interactive_login,
        )
        set_job_state(
            job_id,
            status="succeeded",
            finished_at=datetime.now().isoformat(),
            markdown_path=str(output_path),
            result={"markdown_path": str(output_path)},
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


def create_company_profile_job(request: CompanyProfileRequest) -> dict[str, Any]:
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
    markdown_path = make_markdown_output_path(request)
    set_job_state(
        job_id,
        job_id=job_id,
        status="queued",
        created_at=created_at,
        request=request.model_dump(),
        markdown_path=str(markdown_path),
        result=None,
        error=None,
    )
    worker = threading.Thread(
        target=run_company_profile_job,
        args=(job_id, request, markdown_path),
        daemon=True,
    )
    worker.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "created_at": created_at,
        "markdown_path": str(markdown_path),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/material-parser/company-profile/jobs")
def company_profile_job(request: CompanyProfileRequest) -> dict[str, Any]:
    return create_company_profile_job(request)


@app.get("/api/material-parser/company-profile/jobs/{job_id}")
def company_profile_job_status(job_id: str) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job
