"""Core service HTTP API.

    make serve      # uvicorn --factory caregap.api.app:create_app

Endpoints:
    GET  /health
    GET  /members/{member_id}/gaps?year=2025
    POST /scripts                    generate, check and store a script for a member
    GET  /scripts/{script_id}
    GET  /reviews                    scripts waiting for human review
    POST /reviews/{script_id}        approve or reject a script
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from caregap.config import Settings, get_settings
from caregap.cost import BudgetExceededError
from caregap.llm.base import ProviderError
from caregap.outreach.generate import ScriptGenerator
from caregap.outreach.profile import DEFAULT_PROFILE
from caregap.outreach.store import NotFoundError, OutreachStore, ReviewConflictError

DEFAULT_YEAR = 2025


class GapOut(BaseModel):
    measure: str
    status: str
    evidence_date: date | None
    reason: str


class ScriptRequest(BaseModel):
    member_id: str
    measurement_year: int = DEFAULT_YEAR


class ViolationOut(BaseModel):
    rule: str
    detail: str


class ScriptOut(BaseModel):
    id: int
    member_id: str
    measurement_year: int
    measures: list[str]
    status: str
    script: str
    violations: list[ViolationOut]
    attempts: int
    prompt_versions: dict[str, str]
    provider: str
    model: str
    created_at: datetime
    reviewer: str | None = None
    review_note: str | None = None


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1)
    note: str | None = None


def _script_out(row: dict[str, Any]) -> ScriptOut:
    return ScriptOut(**{**row, "attempts": len(row["attempts"])})


def create_app(
    settings: Settings | None = None,
    *,
    store: OutreachStore | None = None,
    generator: ScriptGenerator | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or OutreachStore(settings.database_url)
    if generator is None:
        from caregap.llm.factory import build_client
        from caregap.prompts import MlflowPromptStore

        client = build_client(settings)
        # Claude spends output tokens on thinking, so it needs more headroom.
        max_tokens = 4000 if settings.llm_provider == "claude" else 600
        generator = ScriptGenerator(
            client,
            MlflowPromptStore(settings.mlflow_tracking_uri),
            DEFAULT_PROFILE,
            max_output_tokens=max_tokens,
        )
    gen = generator

    app = FastAPI(title="CareGap Voice core service", version="0.1.0")

    @app.exception_handler(ProviderError)
    def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"detail": f"model provider unavailable: {exc}"}, status_code=503)

    @app.exception_handler(BudgetExceededError)
    def budget_exceeded(_: Request, exc: BudgetExceededError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=402)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "provider": gen.client.provider.name, "model": gen.client.model}

    @app.get("/members/{member_id}/gaps")
    def member_gaps(member_id: str, year: int = DEFAULT_YEAR) -> list[GapOut]:
        try:
            store.member(member_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return [GapOut(**vars(g)) for g in store.gaps(member_id, year)]

    @app.post("/scripts", status_code=201)
    def create_script(req: ScriptRequest) -> ScriptOut:
        try:
            member = store.member(req.member_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        open_measures = [
            g.measure for g in store.gaps(req.member_id, req.measurement_year) if g.status == "gap"
        ]
        if not open_measures:
            raise HTTPException(409, f"member {req.member_id} has no open gaps")
        outcome = gen.generate(member, open_measures)
        script_id = store.save_script(outcome, req.measurement_year)
        return _script_out(store.script(script_id))

    @app.get("/scripts/{script_id}")
    def get_script(script_id: int) -> ScriptOut:
        try:
            return _script_out(store.script(script_id))
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/reviews")
    def reviews(limit: int = 100) -> list[ScriptOut]:
        return [_script_out(r) for r in store.pending_reviews(limit)]

    @app.post("/reviews/{script_id}")
    def review(script_id: int, req: ReviewRequest) -> ScriptOut:
        try:
            store.review(
                script_id, approve=req.decision == "approve", reviewer=req.reviewer, note=req.note
            )
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _script_out(store.script(script_id))

    return app
