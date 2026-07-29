from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.network_schemas import CommandAckRequest, EventBatchRequest
from app.services.network_ingest import (
    NetworkIngestError,
    acknowledge_command,
    ingest_events,
    list_unacknowledged_commands,
    validate_uuid,
)
from app.services.node_auth import NodeAuthContext, NodeAuthError, authenticate_bearer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["franchise-node-api"])


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "").strip()
    return supplied[:128] if supplied else str(uuid4())


def _response(
    *,
    request_id: str,
    data=None,
    error=None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = {"X-Request-ID": request_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        {
            "data": data,
            "error": error,
            "request_id": request_id,
        },
        status_code=status_code,
        headers=response_headers,
    )


def _error_response(
    request_id: str,
    *,
    status_code: int,
    code: str,
    message: str,
    details=None,
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return _response(
        request_id=request_id,
        data=None,
        error=error,
        status_code=status_code,
        headers=headers,
    )


def _validation_details(exc: ValidationError) -> list[dict[str, object]]:
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors(include_url=False)
    ]


def _authenticate(request: Request, db: Session) -> NodeAuthContext:
    # Production deployments terminate TLS at the host/reverse proxy. The
    # credential is accepted only in Authorization, never query/form data.
    return authenticate_bearer(db, request.headers.get("authorization"))


def _known_error(request_id: str, exc: Exception) -> JSONResponse:
    if isinstance(exc, NodeAuthError):
        return _error_response(
            request_id,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
        )
    if isinstance(exc, NetworkIngestError):
        return _error_response(
            request_id,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )
    raise TypeError("not a known node API error")


@router.post("/events")
async def post_events(request: Request, db: Session = Depends(get_db)):
    request_id = _request_id(request)
    try:
        context = _authenticate(request, db)
    except NodeAuthError as exc:
        db.rollback()
        return _known_error(request_id, exc)

    settings = get_settings()
    max_body_bytes = int(getattr(settings, "node_api_max_body_bytes", 5 * 1024 * 1024))
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > max_body_bytes:
                db.rollback()
                return _error_response(
                    request_id,
                    status_code=413,
                    code="BODY_TOO_LARGE",
                    message="The event request body is too large.",
                )
        except ValueError:
            db.rollback()
            return _error_response(
                request_id,
                status_code=400,
                code="INVALID_CONTENT_LENGTH",
                message="Content-Length must be an integer.",
            )

    body = await request.body()
    if len(body) > max_body_bytes:
        db.rollback()
        return _error_response(
            request_id,
            status_code=413,
            code="BODY_TOO_LARGE",
            message="The event request body is too large.",
        )
    try:
        event_request = EventBatchRequest.model_validate_json(body)
    except ValidationError as exc:
        db.rollback()
        return _error_response(
            request_id,
            status_code=422,
            code="VALIDATION_ERROR",
            message="The event request is invalid.",
            details=_validation_details(exc),
        )

    max_items = int(getattr(settings, "node_api_max_event_items", 5000))
    item_count = sum(len(event.items) for event in event_request.events)
    if item_count > max_items:
        db.rollback()
        return _error_response(
            request_id,
            status_code=413,
            code="TOO_MANY_ITEMS",
            message=f"At most {max_items} items may be submitted in one request.",
        )

    try:
        acknowledgements = ingest_events(db, context.node, event_request)
        last_sequence = context.node.last_sequence
        db.commit()
    except (NodeAuthError, NetworkIngestError) as exc:
        db.rollback()
        return _known_error(request_id, exc)
    except IntegrityError:
        db.rollback()
        return _error_response(
            request_id,
            status_code=409,
            code="CONCURRENT_EVENT_CONFLICT",
            message="The node sequence changed concurrently; retry from the last acknowledged sequence.",
        )
    except Exception:
        db.rollback()
        logger.exception("Unexpected node event ingestion failure; request_id=%s", request_id)
        return _error_response(
            request_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="The event could not be processed.",
        )

    return _response(
        request_id=request_id,
        status_code=200,
        data={
            "acknowledgements": acknowledgements,
            "last_sequence": last_sequence,
        },
        error=None,
    )


@router.get("/node")
def get_node(request: Request, db: Session = Depends(get_db)):
    request_id = _request_id(request)
    try:
        context = _authenticate(request, db)
        node = context.node
        data = {
            "public_id": node.public_id,
            "code": node.code,
            "name": node.name,
            "location": node.location,
            "active": node.active,
            "tally_godown_name": node.tally_godown_name,
            "last_sequence": node.last_sequence,
            "next_sequence": node.last_sequence + 1,
            "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
        }
        db.commit()
    except NodeAuthError as exc:
        db.rollback()
        return _known_error(request_id, exc)
    return _response(request_id=request_id, data=data, error=None)


@router.get("/commands")
def get_commands(
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = _request_id(request)
    try:
        context = _authenticate(request, db)
        raw_limit = request.query_params.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise NetworkIngestError(
                422,
                "INVALID_LIMIT",
                "limit must be an integer from 1 to 100.",
            ) from exc
        if not 1 <= limit <= 100:
            raise NetworkIngestError(
                422,
                "INVALID_LIMIT",
                "limit must be an integer from 1 to 100.",
            )
        commands = list_unacknowledged_commands(db, context.node, limit=limit)
        db.commit()
    except (NodeAuthError, NetworkIngestError) as exc:
        db.rollback()
        return _known_error(request_id, exc)
    return _response(
        request_id=request_id,
        data={"commands": commands},
        error=None,
    )


@router.patch("/commands/{command_public_id}")
async def patch_command(
    command_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = _request_id(request)
    try:
        context = _authenticate(request, db)
        normalized_id = validate_uuid(command_public_id, field="command_public_id")
        body = await request.body()
        if body:
            CommandAckRequest.model_validate_json(body)
        command = acknowledge_command(db, context.node, normalized_id)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _error_response(
            request_id,
            status_code=422,
            code="VALIDATION_ERROR",
            message="The command acknowledgement is invalid.",
            details=_validation_details(exc),
        )
    except (NodeAuthError, NetworkIngestError) as exc:
        db.rollback()
        return _known_error(request_id, exc)
    except Exception:
        db.rollback()
        logger.exception("Unexpected command acknowledgement failure; request_id=%s", request_id)
        return _error_response(
            request_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="The command could not be acknowledged.",
        )
    return _response(request_id=request_id, data={"command": command}, error=None)
