"""Core service HTTP API.

    make serve      # uvicorn --factory caregap.api.app:create_app

Endpoints:
    GET  /health
    GET  /members/{member_id}/gaps?year=2025
    POST /scripts                    generate, check and store a script for a member
    GET  /scripts/{script_id}
    GET  /reviews                    scripts waiting for human review
    POST /reviews/{script_id}        approve or reject a script
    POST /scripts/{script_id}/audio  render a compliant or approved script to speech
    GET  /scripts/{script_id}/audio  the latest rendered WAV

Campaigns (Phase 5; called by n8n, see caregap.campaign.service):
    POST /campaigns                                   select members for a new campaign
    GET  /campaigns/{id}                              funnel summary
    GET  /work                                        prepare/send items that are due now
    POST /campaigns/{id}/members/{member_id}/prepare  generate the member's script
    POST /campaigns/{id}/members/{member_id}/send     claim a call; returns the Twilio request
    POST /attempts/{id}/dispatched                    Twilio accepted the call
    POST /attempts/{id}/dispatch-failed               Twilio refused the call
    POST /twiml/attempts/{id}                         TwiML for an answered call (Twilio)
    POST /telephony/events                            a Twilio callback relayed by n8n
    POST /alerts                                      a failed n8n workflow
    GET  /notifications/outbox                        escalations to deliver
    POST /escalations/{id}/notified                   mark one delivered
    GET  /escalations                                 open escalations
    POST /notifications                               local log sink for notifications

Callback Q&A (Phase 7; see caregap.rag.agent):
    POST /callbacks/questions                         answer from the plan's benefits, or hand off

Agent tools (Phase 9; called by caregap.mcp, rules in caregap.members):
    GET  /members/{member_id}?year=2025               plan, allowed fields and the plan's gaps
    POST /members/{member_id}/interactions            log a contact with the member
    POST /escalations                                 ask for a human (idempotent while open)
    POST /benefits/search                             benefit lines as written, nearest first
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel, Field

from caregap.campaign.clock import Clock, build_clock
from caregap.campaign.service import (
    CampaignService,
    EventConflictError,
    InvalidSignatureError,
    TelephonyConfig,
)
from caregap.config import Settings, get_settings
from caregap.cost import BudgetExceededError
from caregap.customers import LANGUAGES, CustomerConfig, customer_for, load_all
from caregap.llm.base import LLMClient, ProviderError
from caregap.members import Channel, MemberService, NoCustomerError, Outcome
from caregap.outreach.generate import ScriptGenerator
from caregap.outreach.profile import OutreachProfile
from caregap.outreach.store import NotFoundError, OutreachStore, ReviewConflictError
from caregap.prompts import PromptStore
from caregap.rag.agent import CallbackAgent, token_limits
from caregap.rag.service import CallbackService
from caregap.rag.store import BenefitRetriever, ChunkStore
from caregap.voice.render import AudioRenderer, NotRenderableError, RenderedAudio, latest_audio

DEFAULT_YEAR = 2025

log = logging.getLogger(__name__)


class GapOut(BaseModel):
    measure: str
    status: str
    evidence_date: date | None
    reason: str


class ScriptRequest(BaseModel):
    member_id: str
    measurement_year: int = DEFAULT_YEAR
    #: Defaults to the customer whose member rule includes the member.
    customer: str | None = None


class ViolationOut(BaseModel):
    rule: str
    detail: str


class ScriptOut(BaseModel):
    id: int
    customer: str
    language: str
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


class AudioOut(BaseModel):
    id: int
    script_id: int
    provider: str
    voice: str
    pronunciation_overrides: bool
    duration_ms: int
    latency_ms: int
    url: str


class CampaignRequest(BaseModel):
    customer: str
    name: str = "campaign"
    size: int = Field(default=20, ge=1, le=5000)
    measurement_year: int = DEFAULT_YEAR
    #: Overrides the profile's retry delay (the demo uses 1 minute).
    retry_delay_minutes: int | None = Field(default=None, ge=0)


class DispatchedRequest(BaseModel):
    call_sid: str = Field(min_length=1)


class DispatchFailedRequest(BaseModel):
    error: str = "unknown error"


class TelephonyEvent(BaseModel):
    """A Twilio callback as n8n received it: query string, form body, signature header."""

    query: dict[str, str]
    body: dict[str, str]
    signature: str = ""


class AlertRequest(BaseModel):
    workflow: str
    node: str | None = None
    message: str
    execution: str | None = None


class Notification(BaseModel):
    text: str


class CallbackQuestion(BaseModel):
    customer: str
    question: str = Field(min_length=1, max_length=1000)
    member_id: str | None = None


class CallbackAnswer(BaseModel):
    id: int
    customer: str
    language: str
    route: str
    outcome: str
    reason: str | None
    #: What the caller hears.
    answer: str
    citations: list[str]
    escalation_id: int | None


class MemberOut(BaseModel):
    member_id: str
    customer: str
    plan_name: str
    language: str
    fields: dict[str, str | int | date | None]
    gaps: list[GapOut]


class InteractionRequest(BaseModel):
    channel: Channel
    outcome: Outcome
    summary: str = Field(min_length=1, max_length=2000)
    source: str = Field(default="mcp", min_length=1, max_length=40)
    measurement_year: int = DEFAULT_YEAR


class EscalationRequest(BaseModel):
    member_id: str
    reason: str = Field(min_length=1, max_length=1000)
    campaign_id: int | None = None
    source: str = Field(default="mcp", min_length=1, max_length=40)
    measurement_year: int = DEFAULT_YEAR


class BenefitSearch(BaseModel):
    customer: str
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)


class BenefitHit(BaseModel):
    id: str
    section: str
    title: str
    text: str
    distance: float


def _audio_out(a: RenderedAudio) -> AudioOut:
    return AudioOut(**{**vars(a), "url": f"/scripts/{a.script_id}/audio"})


def _script_out(row: dict[str, Any]) -> ScriptOut:
    return ScriptOut(**{**row, "attempts": len(row["attempts"])})


def create_app(
    settings: Settings | None = None,
    *,
    store: OutreachStore | None = None,
    llm: LLMClient | None = None,
    prompts: PromptStore | None = None,
    renderer: AudioRenderer | None = None,
    clock: Clock | None = None,
    customers: Mapping[str, CustomerConfig] | None = None,
    embeddings: Embeddings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or OutreachStore(settings.database_url)
    # Fail at startup, not mid-campaign, if a config is invalid.
    configs = dict(customers) if customers is not None else load_all(settings.customers_dir)
    if llm is None:
        from caregap.llm.factory import build_client

        llm = build_client(settings)
    if prompts is None:
        from caregap.prompts import MlflowPromptStore

        prompts = MlflowPromptStore(settings.mlflow_tracking_uri)
    # Claude spends output tokens on thinking, so it needs more headroom.
    max_tokens = 4000 if settings.llm_provider == "claude" else 600
    client, prompt_store = llm, prompts
    generators: dict[str, ScriptGenerator] = {}

    def generator_for(profile: OutreachProfile) -> ScriptGenerator:
        key = profile.model_dump_json()
        if key not in generators:
            generators[key] = ScriptGenerator(
                client, prompt_store, profile, max_output_tokens=max_tokens
            )
        return generators[key]

    def customer(customer_id: str) -> CustomerConfig:
        if customer_id not in configs:
            raise HTTPException(404, f"customer {customer_id!r} is not configured")
        return configs[customer_id]

    cfg = settings
    renderers: list[AudioRenderer] = [renderer] if renderer else []

    def get_renderer() -> AudioRenderer:
        # The TTS model loads on first use, so the service starts without model files.
        if not renderers:
            from caregap.voice.render import build_tts

            renderers.append(
                AudioRenderer(
                    cfg.database_url,
                    build_tts(cfg.data_dir, overrides=cfg.tts_pronunciation_overrides),
                    cfg.data_dir / "audio",
                    overrides_enabled=cfg.tts_pronunciation_overrides,
                )
            )
        return renderers[0]

    campaigns = CampaignService(
        settings.database_url,
        outreach=store,
        generators=generator_for,
        renderer=get_renderer,
        customers=configs,
        telephony=TelephonyConfig(
            public_base_url=settings.public_base_url,
            callback_url=settings.twilio_callback_url,
            api_base_url=settings.twilio_api_base_url,
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token.get_secret_value(),
            from_number=settings.twilio_from_number,
        ),
        clock=clock or build_clock(settings.campaign_clock),
    )

    callback_services: list[CallbackService] = []
    chunks = ChunkStore(cfg.database_url)
    embedders: dict[str, Embeddings] = {}

    def embeddings_for(purpose: str) -> Embeddings:
        # Built on first use, like the TTS model: the service starts without Ollama.
        if embeddings is not None:
            return embeddings
        if purpose not in embedders:
            from caregap.llm.embeddings import LangChainEmbeddings
            from caregap.llm.factory import build_embeddings

            embedders[purpose] = LangChainEmbeddings(build_embeddings(cfg), purpose=purpose)
        return embedders[purpose]

    def callbacks() -> CallbackService:
        if not callback_services:
            emb = embeddings_for("callback")
            agent = CallbackAgent(
                client, prompt_store, configs,
                lambda c: BenefitRetriever(store=chunks, embeddings=emb, customer=c),
                **token_limits(cfg.llm_provider),
            )  # fmt: skip
            callback_services.append(CallbackService(cfg.database_url, agent))
        return callback_services[0]

    members = MemberService(settings.database_url, store, configs, campaigns.clock)

    app = FastAPI(title="CareGap Voice core service", version="0.1.0")

    @app.exception_handler(NotFoundError)
    def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(NoCustomerError)
    def no_customer(_: Request, exc: NoCustomerError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(InvalidSignatureError)
    def bad_signature(_: Request, exc: InvalidSignatureError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(EventConflictError)
    def conflict(_: Request, exc: EventConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ProviderError)
    def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"detail": f"model provider unavailable: {exc}"}, status_code=503)

    @app.exception_handler(BudgetExceededError)
    def budget_exceeded(_: Request, exc: BudgetExceededError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=402)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "provider": client.provider.name,
            "model": client.model,
            "clock": campaigns.clock.now().isoformat(),
        }

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
        if req.customer is not None:
            c = customer(req.customer)
            if not c.members.includes(member.birth_date, req.measurement_year):
                raise HTTPException(409, f"member {req.member_id} is not a {c.id} member")
        else:
            found = customer_for(configs, member.birth_date, req.measurement_year)
            if found is None:
                raise HTTPException(409, f"member {req.member_id} has no configured customer")
            c = found
        open_measures = [
            g.measure
            for g in store.gaps(req.member_id, req.measurement_year)
            if g.status == "gap" and g.measure in c.measures
        ]
        if not open_measures:
            raise HTTPException(409, f"member {req.member_id} has no open gaps for {c.id}")
        profile = c.profile(c.language_for(member.language))
        outcome = generator_for(profile).generate(member, open_measures)
        script_id = store.save_script(outcome, req.measurement_year, customer=c.id)
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

    @app.post("/scripts/{script_id}/audio", status_code=201)
    def render_audio(script_id: int) -> AudioOut:
        try:
            script = store.script(script_id)
            lang = customer(script["customer"]).languages[script["language"]]
            return _audio_out(
                get_renderer().render(
                    script, voice=lang.voice, lang=LANGUAGES[script["language"]].tts_lang
                )
            )
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except NotRenderableError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/scripts/{script_id}/audio")
    def get_audio(script_id: int) -> FileResponse:
        audio = latest_audio(cfg.database_url, script_id)
        if audio is None:
            raise HTTPException(404, f"no audio rendered for script {script_id}")
        return FileResponse(audio.path, media_type="audio/wav")

    # --- campaigns ---------------------------------------------------------------

    @app.post("/campaigns", status_code=201)
    def create_campaign(req: CampaignRequest) -> dict[str, Any]:
        customer(req.customer)
        return campaigns.create(
            customer=req.customer,
            name=req.name,
            size=req.size,
            measurement_year=req.measurement_year,
            retry_delay_minutes=req.retry_delay_minutes,
        )

    @app.get("/campaigns/{campaign_id}")
    def campaign_summary(campaign_id: int) -> dict[str, Any]:
        return campaigns.summary(campaign_id)

    @app.get("/work")
    def work(prepare_limit: int = 4, send_limit: int = 20) -> dict[str, Any]:
        return {"items": campaigns.work(prepare_limit=prepare_limit, send_limit=send_limit)}

    @app.post("/campaigns/{campaign_id}/members/{member_id}/prepare")
    def prepare(campaign_id: int, member_id: str) -> dict[str, Any]:
        return campaigns.prepare(campaign_id, member_id)

    @app.post("/campaigns/{campaign_id}/members/{member_id}/send")
    def send(campaign_id: int, member_id: str) -> dict[str, Any]:
        return campaigns.send(campaign_id, member_id)

    @app.post("/attempts/{attempt_id}/dispatched")
    def dispatched(attempt_id: int, req: DispatchedRequest) -> dict[str, str]:
        campaigns.dispatched(attempt_id, req.call_sid)
        return {"status": "ok"}

    @app.post("/attempts/{attempt_id}/dispatch-failed")
    def dispatch_failed(attempt_id: int, req: DispatchFailedRequest) -> dict[str, str]:
        campaigns.dispatch_failed(attempt_id, req.error)
        return {"status": "ok"}

    @app.post("/twiml/attempts/{attempt_id}")
    async def twiml(attempt_id: int, request: Request) -> Response:
        params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        doc = campaigns.twiml(attempt_id, params, request.headers.get("x-twilio-signature", ""))
        return Response(doc, media_type="application/xml")

    @app.post("/telephony/events")
    def telephony_event(ev: TelephonyEvent) -> dict[str, str | None]:
        return {"twiml": campaigns.telephony_event(ev.query, ev.body, ev.signature)}

    @app.post("/alerts", status_code=201)
    def alert(req: AlertRequest) -> dict[str, int]:
        return {"escalation_id": campaigns.alert(**req.model_dump())}

    @app.get("/notifications/outbox")
    def outbox(limit: int = 50) -> dict[str, Any]:
        return {"url": cfg.notify_webhook_url, "items": campaigns.outbox(limit)}

    @app.post("/escalations/{escalation_id}/notified")
    def notified(escalation_id: int) -> dict[str, str]:
        campaigns.mark_notified(escalation_id)
        return {"status": "ok"}

    @app.get("/escalations")
    def escalations(open_only: bool = True) -> list[dict[str, Any]]:
        return campaigns.escalations(open_only=open_only)

    @app.post("/callbacks/questions")
    def callback_question(q: CallbackQuestion) -> CallbackAnswer:
        customer(q.customer)
        qid, a, escalation_id = callbacks().ask(q.customer, q.question, q.member_id)
        return CallbackAnswer(
            id=qid, customer=a.customer, language=a.language, route=a.route,
            outcome=a.outcome, reason=a.reason, answer=a.text, citations=list(a.citations),
            escalation_id=escalation_id,
        )  # fmt: skip

    # --- agent tools (MCP) ------------------------------------------------------------

    @app.get("/members/{member_id}")
    def member_view(member_id: str, year: int = DEFAULT_YEAR) -> MemberOut:
        v = members.view(member_id, year)
        return MemberOut(**{**vars(v), "gaps": [GapOut(**vars(g)) for g in v.gaps]})

    @app.post("/members/{member_id}/interactions", status_code=201)
    def log_interaction(member_id: str, req: InteractionRequest) -> dict[str, int]:
        iid = members.log_interaction(
            member_id, channel=req.channel, outcome=req.outcome, summary=req.summary,
            source=req.source, year=req.measurement_year,
        )  # fmt: skip
        return {"interaction_id": iid}

    @app.post("/escalations")
    def escalate(req: EscalationRequest, response: Response) -> dict[str, int | bool]:
        eid, created = members.escalate(
            req.member_id, reason=req.reason, source=req.source, year=req.measurement_year,
            campaign_id=req.campaign_id,
        )  # fmt: skip
        response.status_code = 201 if created else 200
        return {"escalation_id": eid, "created": created}

    @app.post("/benefits/search")
    def search_benefits(req: BenefitSearch) -> list[BenefitHit]:
        customer(req.customer)
        vector = embeddings_for("benefit-search").embed_query(req.query)
        return [
            BenefitHit(id=c.citation, section=c.section, title=c.title, text=c.text, distance=d)
            for c, d in chunks.search(req.customer, vector, req.k)
        ]

    @app.post("/notifications", status_code=202)
    def notification_sink(n: Notification) -> dict[str, str]:
        """Stands in for a Slack incoming webhook: appends to data/notifications.log."""
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        line = {"received_at": datetime.now(UTC).isoformat(), "text": n.text}
        with (cfg.data_dir / "notifications.log").open("a") as f:
            f.write(json.dumps(line) + "\n")
        log.warning("notification: %s", n.text)
        return {"status": "accepted"}

    return app
