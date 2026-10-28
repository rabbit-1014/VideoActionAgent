"""Thin local interaction surface over the existing authenticated task API."""

from copy import deepcopy
from hmac import compare_digest
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import create_app
from .content_intake import IntakeError
from .local_flow import FlowError, LocalFlow
from .local_job_store import JobStoreError
from .notes_checks import NotesCheckError
from .notes_requirements import NotesRequirementsError
from .notes_service import NotesServiceError
from .saved_reference import ReferenceUnavailable

WEB_ROOT = Path(__file__).parent / "web"
SESSION_COOKIE = "vaa_ui_session"


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    source: str = Field(min_length=1, max_length=4096)
    allow_local_audio_read: StrictBool


class NotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    source_id: str = Field(min_length=1, max_length=48)
    user_request: str = Field(min_length=1, max_length=4000)
    allow_model_text: StrictBool
    output_kind: Literal["structured_notes", "method_list", "source_answer"] = "structured_notes"


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    source: str = Field(min_length=1, max_length=4096)
    user_request: str = Field(min_length=1, max_length=4000)
    output_kind: Literal["structured_notes", "method_list", "source_answer"] = "structured_notes"
    allow_local_audio_read: StrictBool
    allow_model_text: StrictBool


class NotesRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_parent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction: str = Field(min_length=1, max_length=4000)
    allow_model_text: StrictBool


class NotesCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    allow_model_text: StrictBool


def create_ui_app(runtime, tokens, worker_status=None, *, source_modes=("subtitle",),
                  processing_status=None, source_preview=None, source_owner=None,
                  source_capture=None, notes_service=None, notes_checks=None, saved_reference=None):
    preview = deepcopy(source_preview)
    if preview is not None and (
        source_owner not in tokens.values() or preview.get("completed") is not False
        or preview.get("formal_task") is not False
        or preview.get("state") != "unverified_source_candidate"
    ):
        raise ValueError("Explicit owner and a source-only candidate required")
    modes = tuple(source_modes)
    if any(mode not in {"subtitle", "audio_transcription"} for mode in modes):
        raise ValueError("Unsupported UI source mode")
    app = create_app(runtime, tokens)
    if source_capture is not None and source_capture.owner not in tokens.values():
        raise ValueError("Capture owner must have an explicit session")
    if notes_service is not None and notes_service.owner not in tokens.values():
        raise ValueError("Notes owner must have an explicit session")
    flows = (LocalFlow(source_capture, notes_service)
             if source_capture and notes_service and notes_service._capture is source_capture
             and source_capture.owner == notes_service.owner else None)
    app.state.video_flows = flows
    if notes_checks is not None and (notes_checks.notes is not notes_service
                                    or notes_checks.owner not in tokens.values()):
        raise ValueError("Checks must reference the same owner-bound notes service")
    app.state.notes_checks = notes_checks
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/ui-static", StaticFiles(directory=WEB_ROOT, check_dir=False), name="ui-static")

    def access(request):
        authorization = request.headers.get("authorization")
        supplied = (authorization[7:] if authorization and authorization.startswith("Bearer ")
                    else request.cookies.get(SESSION_COOKIE) if not authorization else None)
        if supplied:
            for token in tokens:
                if compare_digest(supplied.encode(), token.encode()):
                    return token
        return None

    @app.middleware("http")
    async def private_local_responses(request, call_next):
        origin = request.headers.get("origin")
        if (request.method not in {"GET", "HEAD", "OPTIONS"} and origin is not None
                and origin != f"{request.url.scheme}://{request.headers.get('host')}"):
            response = JSONResponse(status_code=403, content={"detail": "Local origin required"})
        else:
            # Refresh uses an HttpOnly session, with the same API owner checks as Bearer access.
            token = access(request)
            if token and not request.headers.get("authorization"):
                request.scope["headers"] = [*request.scope["headers"],
                                            (b"authorization", ("Bearer " + token).encode())]
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/app")

    @app.get("/app", include_in_schema=False)
    def page():
        return FileResponse(WEB_ROOT / "index.html", media_type="text/html")

    @app.get("/app/status", include_in_schema=False)
    def status():
        state = worker_status() if worker_status is not None else {}
        return {"execution_enabled": state.get("running") is True,
                "notes_enabled": notes_service is not None and notes_service.current()["enabled"],
                "source_capture_enabled": source_capture is not None
                and source_capture.current()["enabled"],
                "capabilities": ["method_list_" + mode for mode in modes]}

    @app.get("/app/session", include_in_schema=False)
    def current_session(request: Request):
        return {"connected": access(request) is not None}

    @app.get("/app/source-candidate", include_in_schema=False)
    def saved_source(request: Request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if preview is None or tokens[token] != source_owner:
            raise HTTPException(404, "Source candidate unavailable")
        return preview

    def capture_service(request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if source_capture is None:
            raise HTTPException(403, "Source capture not enabled")
        if tokens[token] != source_capture.owner:
            raise HTTPException(404, "Source capture unavailable")
        return source_capture

    def intake_call(action):
        try:
            return action()
        except (IntakeError, JobStoreError) as exc:
            status = (503 if exc.code in {"job_storage_unavailable", "intake_storage_unavailable"} else
                      404 if exc.code == "intake_not_found" else
                      422 if exc.code == "invalid_source" else
                      403 if exc.code == "audio_permission_required" else 409)
            raise HTTPException(status, exc.code) from None

    @app.get("/app/intake", include_in_schema=False)
    def current_intake(request: Request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if source_capture is None:
            return {"enabled": False, "job": None}
        return capture_service(request).current()

    @app.post("/app/intake", include_in_schema=False)
    def start_intake(body: CaptureRequest, request: Request):
        manager = capture_service(request)
        return intake_call(lambda: manager.start(body.request_id, body.source,
                           allow_local_audio_read=body.allow_local_audio_read))

    @app.get("/app/intake/history", include_in_schema=False)
    def intake_history(request: Request):
        return intake_call(lambda: capture_service(request).history())

    @app.get("/app/intake/{job_id}", include_in_schema=False)
    def get_intake(job_id: UUID, request: Request):
        manager = capture_service(request)
        return intake_call(lambda: manager.get(job_id))

    @app.post("/app/intake/{job_id}/cancel", include_in_schema=False)
    def cancel_intake(job_id: UUID, request: Request):
        manager = capture_service(request)
        return intake_call(lambda: manager.cancel(job_id))

    @app.get("/app/intake/{job_id}/candidate", include_in_schema=False)
    def intake_candidate(job_id: UUID, request: Request):
        manager = capture_service(request)
        return intake_call(lambda: manager.candidate(job_id))

    @app.get("/app/tasks/{task_id}/processing", include_in_schema=False)
    def processing(task_id: UUID, request: Request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        owner = tokens[token]
        task = runtime.get(owner, task_id)
        hint = processing_status(owner, task) if processing_status is not None else None
        return {"observation": hint}

    def notes_manager(request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if notes_service is None:
            raise HTTPException(403, "Notes processing not enabled")
        if tokens[token] != notes_service.owner:
            raise HTTPException(404, "Notes unavailable")
        return notes_service

    def notes_call(action):
        try:
            return action()
        except (NotesServiceError, JobStoreError) as exc:
            code = exc.code
            status = (503 if code in {"job_storage_unavailable", "notes_storage_unavailable"} else
                      404 if code == "notes_candidate_unavailable" else
                      403 if code == "model_text_permission_required" else
                      422 if code == "invalid_notes_request" else 409)
            raise HTTPException(status, code) from None

    def check_manager(request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if notes_checks is None:
            raise HTTPException(404, "Checks not enabled")
        if tokens[token] != notes_checks.owner:
            raise HTTPException(404, "Checks unavailable")
        return notes_checks

    def check_call(action):
        try:
            return action()
        except (NotesCheckError, NotesRequirementsError, NotesServiceError, JobStoreError) as exc:
            code = exc.code
            status = (503 if code.endswith("storage_unavailable") else
                      404 if code in {"check_unavailable", "notes_candidate_unavailable"} else
                      403 if code == "model_text_permission_required" else
                      422 if code in {"invalid_check_request", "notes_check_kind_unsupported"} else 409)
            raise HTTPException(status, code) from None

    @app.get("/app/notes-checks", include_in_schema=False)
    def current_checks(request: Request, candidate_id: UUID | None = None):
        if access(request) is None:
            raise HTTPException(401, "Authentication required")
        if notes_checks is None:
            return {"enabled": False, "scope_id": None, "job": None}
        manager = check_manager(request)
        return {"enabled": True, "scope_id": manager.scope_id,
                "job": check_call(lambda: manager.current(candidate_id))}

    @app.post("/app/notes/{candidate_id}/checks", include_in_schema=False)
    def start_check(candidate_id: UUID, body: NotesCheckRequest, request: Request):
        manager = check_manager(request)
        return check_call(lambda: manager.start(body.request_id, candidate_id,
                                               allow_model_text=body.allow_model_text))

    @app.get("/app/notes-checks/{job_id}", include_in_schema=False)
    def get_check(job_id: UUID, request: Request):
        manager = check_manager(request)
        return check_call(lambda: manager.get(job_id))

    @app.post("/app/notes-checks/{job_id}/cancel", include_in_schema=False)
    def cancel_check(job_id: UUID, request: Request):
        manager = check_manager(request)
        return check_call(lambda: manager.cancel(job_id))

    @app.get("/app/notes-checks/{job_id}/result", include_in_schema=False)
    def check_result(job_id: UUID, request: Request):
        manager = check_manager(request)
        return check_call(lambda: manager.result(job_id))

    def flow_manager(request):
        token = access(request)
        if token is None:
            raise HTTPException(401, "Authentication required")
        if flows is None:
            raise HTTPException(403, "Linked processing not enabled")
        if tokens[token] != flows.owner:
            raise HTTPException(404, "Flow unavailable")
        return flows

    def flow_call(action):
        try:
            return action()
        except (FlowError, JobStoreError) as exc:
            status = (503 if exc.code == "job_storage_unavailable" else
                      404 if exc.code == "flow_not_found" else
                      403 if exc.code == "flow_permission_required" else
                      422 if exc.code == "invalid_flow_request" else 409)
            raise HTTPException(status, exc.code) from None

    @app.get("/app/flows", include_in_schema=False)
    def current_flow(request: Request):
        if access(request) is None:
            raise HTTPException(401, "Authentication required")
        if flows is None:
            return {"enabled": False, "flow": None, "scope_id": None}
        return flow_call(lambda: flow_manager(request).current())

    @app.post("/app/flows", include_in_schema=False)
    def start_flow(body: FlowRequest, request: Request):
        return flow_call(lambda: flow_manager(request).start(**body.model_dump()))

    @app.get("/app/flows/history", include_in_schema=False)
    def flow_history(request: Request):
        return flow_call(lambda: flow_manager(request).history())

    @app.get("/app/flows/{flow_id}", include_in_schema=False)
    def get_flow(flow_id: UUID, request: Request):
        return flow_call(lambda: flow_manager(request).get(flow_id))

    @app.post("/app/flows/{flow_id}/cancel", include_in_schema=False)
    def cancel_flow(flow_id: UUID, request: Request):
        return flow_call(lambda: flow_manager(request).cancel(flow_id))

    @app.get("/app/notes/sources", include_in_schema=False)
    def note_sources(request: Request):
        return notes_call(lambda: notes_manager(request).sources())

    @app.get("/app/notes", include_in_schema=False)
    def current_notes(request: Request):
        if access(request) is None:
            raise HTTPException(401, "Authentication required")
        if notes_service is None:
            return {"enabled": False, "job": None}
        return notes_manager(request).current()

    @app.post("/app/notes", include_in_schema=False)
    def start_notes(body: NotesRequest, request: Request):
        manager = notes_manager(request)
        return notes_call(lambda: manager.start(body.request_id, body.source_id, body.user_request,
                          allow_model_text=body.allow_model_text, output_kind=body.output_kind))

    @app.get("/app/notes/history", include_in_schema=False)
    def notes_history(request: Request):
        return notes_call(lambda: notes_manager(request).history())

    @app.get("/app/notes/{job_id}", include_in_schema=False)
    def get_notes(job_id: UUID, request: Request):
        manager = notes_manager(request)
        return notes_call(lambda: manager.get(job_id))

    @app.post("/app/notes/{job_id}/cancel", include_in_schema=False)
    def cancel_notes(job_id: UUID, request: Request):
        manager = notes_manager(request)
        return notes_call(lambda: manager.cancel(job_id))

    @app.post("/app/notes/{job_id}/revisions", include_in_schema=False)
    def revise_notes(job_id: UUID, body: NotesRevisionRequest, request: Request):
        manager = notes_manager(request)
        return notes_call(lambda: manager.revise(job_id, body.request_id,
                          body.expected_parent_fingerprint, body.instruction,
                          allow_model_text=body.allow_model_text))

    @app.get("/app/notes/{job_id}/candidate", include_in_schema=False)
    def notes_candidate(job_id: UUID, request: Request):
        manager = notes_manager(request)
        candidate = notes_call(lambda: manager.candidate(job_id))
        if saved_reference is not None:
            candidate = {**candidate, "independent_reference":
                         saved_reference.metadata(job_id, candidate)}
        return candidate

    def read_reference(job_id, request):
        manager = notes_manager(request)
        candidate = notes_call(lambda: manager.candidate(job_id))
        if saved_reference is None:
            raise HTTPException(404, "reference_not_associated")
        try:
            payload = saved_reference.read(job_id, candidate)
        except ReferenceUnavailable as exc:
            raise HTTPException(exc.status, exc.code) from None
        return payload, candidate

    @app.get("/app/notes/{job_id}/reference", include_in_schema=False)
    def notes_reference(job_id: UUID, request: Request):
        payload, _ = read_reference(job_id, request)
        return {"reference": saved_reference.describe(),
                "markdown": payload.decode("utf-8")}

    @app.get("/app/notes/{job_id}/reference/download", include_in_schema=False)
    def download_notes_reference(job_id: UUID, request: Request):
        payload, _ = read_reference(job_id, request)
        return Response(payload, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="subtitle-reference.md"'})

    @app.post("/app/session", include_in_schema=False)
    def establish_session(request: Request):
        token = access(request)
        if token is None:
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        response = JSONResponse({"connected": True})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    return app
