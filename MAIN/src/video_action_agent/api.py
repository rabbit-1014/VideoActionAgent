"""Local API. Server-configured credentials establish ownership, never request fields."""

import os
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .models import ArtifactAcceptanceRequest, CancelRequest, RevisionRequest, TaskInput
from .runtime import Conflict, NotFound, Runtime


def create_app(runtime, tokens, dispose_on_shutdown=False):
    if not tokens or any(not key or not value for key, value in tokens.items()):
        raise ValueError("Explicit server-owned credentials required")
    tokens = dict(tokens)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if dispose_on_shutdown:
            runtime.engine.dispose()

    app = FastAPI(title="Video action task runtime", lifespan=lifespan)

    def principal(authorization: Annotated[str | None, Header()] = None):
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization[7:].encode()
            for token, owner in tokens.items():
                if compare_digest(supplied, token.encode()):
                    return owner
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(NotFound)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Task not found"})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Task storage unavailable"})

    @app.get("/health")
    def health():
        with runtime.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "video_handler_ready": False}

    @app.post("/tasks", status_code=202)
    def submit(
        body: TaskInput,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
        owner: Annotated[str, Depends(principal)],
    ):
        return runtime.submit(owner, idempotency_key, body.model_dump())

    @app.get("/tasks/{task_id}")
    def get_task(task_id: UUID, owner: Annotated[str, Depends(principal)]):
        return runtime.get(owner, task_id)

    @app.get("/tasks/{task_id}/artifact")
    def get_artifact(task_id: UUID, owner: Annotated[str, Depends(principal)]):
        return runtime.get_artifact(owner, task_id)

    @app.post("/tasks/{task_id}/artifact/accept")
    def accept_artifact(
        task_id: UUID, body: ArtifactAcceptanceRequest,
        owner: Annotated[str, Depends(principal)],
    ):
        return runtime.accept_artifact(
            owner, task_id, body.artifact_id, body.expected_revision
        )

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: UUID, body: CancelRequest, owner: Annotated[str, Depends(principal)]):
        return runtime.cancel(owner, task_id, body.expected_revision)

    @app.post("/tasks/{task_id}/revisions")
    def revise_task(
        task_id: UUID, body: RevisionRequest, owner: Annotated[str, Depends(principal)]
    ):
        return runtime.revise(owner, task_id, body.expected_revision, body.request.model_dump())

    return app


def create_env_app():
    token = os.environ["VIDEO_API_TOKEN"]
    owner = os.environ["VIDEO_OWNER_ID"]
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    return create_app(Runtime(engine), {token: owner}, dispose_on_shutdown=True)
