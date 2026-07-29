from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FranchiseNode, NodeCredential, Role, User, utc_now
from app.security import hash_password

API_KEY_PREFIX = "setuora-node"
_DUMMY_SECRET_HASH = hashlib.sha256(b"setuora-node-invalid-secret").hexdigest()
_rate_limit_lock = Lock()
_request_times: dict[str, deque[float]] = defaultdict(deque)


class NodeAuthError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProvisionedCredential:
    credential: NodeCredential
    api_key: str


@dataclass(frozen=True)
class NodeAuthContext:
    node: FranchiseNode
    credential: NodeCredential


def hash_node_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ensure_node_service_user(db: Session, node: FranchiseNode) -> User:
    if node.service_user_id:
        service_user = db.get(User, node.service_user_id)
        if service_user is not None:
            return service_user

    stem = f"node_{node.code.lower()}"[:70]
    username = stem
    counter = 1
    while db.scalar(select(User.id).where(User.username == username)) is not None:
        counter += 1
        username = f"{stem[: 70 - len(str(counter))]}{counter}"

    # This inactive account exists only to attribute mirrored inventory rows.
    # It cannot log in, and its randomly generated password is discarded.
    service_user = User(
        username=username,
        password_hash=hash_password(secrets.token_urlsafe(48)),
        role=Role.ADMIN.value,
        active=False,
        must_change_password=False,
    )
    db.add(service_user)
    db.flush()
    node.service_user_id = service_user.id
    return service_user


def create_franchise_node(
    db: Session,
    *,
    code: str,
    name: str,
    location: str,
    tally_godown_name: str | None = None,
    active: bool = True,
    commit: bool = True,
) -> FranchiseNode:
    node = FranchiseNode(
        code=code.strip().upper(),
        name=name.strip().upper(),
        location=location.strip().upper(),
        tally_godown_name=tally_godown_name.strip() if tally_godown_name else None,
        active=active,
    )
    db.add(node)
    db.flush()
    ensure_node_service_user(db, node)
    if commit:
        db.commit()
        db.refresh(node)
    return node


def provision_node_credential(
    db: Session,
    node: FranchiseNode,
    *,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> ProvisionedCredential:
    if not node.active:
        raise ValueError("cannot provision a credential for an inactive franchise")

    # key_id is intentionally parseable/indexable. The high-entropy secret is
    # returned once and only its SHA-256 digest is stored.
    key_id = secrets.token_urlsafe(12)
    secret = secrets.token_urlsafe(32)
    credential = NodeCredential(
        franchise_id=node.id,
        key_id=key_id,
        secret_hash=hash_node_secret(secret),
        expires_at=expires_at,
    )
    db.add(credential)
    db.flush()
    if commit:
        db.commit()
        db.refresh(credential)
    return ProvisionedCredential(
        credential=credential,
        api_key=f"{API_KEY_PREFIX}.{key_id}.{secret}",
    )


def rotate_node_credential(
    db: Session,
    node: FranchiseNode,
    *,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> ProvisionedCredential:
    now = utc_now()
    credentials = db.scalars(
        select(NodeCredential).where(
            NodeCredential.franchise_id == node.id,
            NodeCredential.revoked_at.is_(None),
        )
    ).all()
    for credential in credentials:
        credential.revoked_at = now
    provisioned = provision_node_credential(
        db,
        node,
        expires_at=expires_at,
        commit=False,
    )
    if commit:
        db.commit()
        db.refresh(provisioned.credential)
    return provisioned


def parse_api_key(value: str) -> tuple[str, str] | None:
    parts = value.split(".", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
        return None
    key_id, secret = parts[1], parts[2]
    if not key_id or len(secret) < 32:
        return None
    return key_id, secret


def _check_rate_limit(key_id: str, now: datetime) -> None:
    settings = get_settings()
    limit = max(1, int(getattr(settings, "node_api_rate_limit_per_minute", 120)))
    current = now.timestamp()
    cutoff = current - 60
    with _rate_limit_lock:
        timestamps = _request_times[key_id]
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= limit:
            raise NodeAuthError(
                429,
                "RATE_LIMITED",
                "Too many requests for this node credential.",
            )
        timestamps.append(current)


def clear_node_rate_limits() -> None:
    """Test/operations helper; credential rotation also changes the rate key."""
    with _rate_limit_lock:
        _request_times.clear()


def authenticate_bearer(
    db: Session,
    authorization: str | None,
    *,
    apply_rate_limit: bool = True,
) -> NodeAuthContext:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer":
        raise NodeAuthError(401, "AUTH_REQUIRED", "A Bearer node API key is required.")

    parsed = parse_api_key(token.strip())
    if parsed is None:
        # Do a constant-time operation even for malformed tokens.
        hmac.compare_digest(_DUMMY_SECRET_HASH, hash_node_secret(token.strip()))
        raise NodeAuthError(401, "INVALID_CREDENTIAL", "The node API key is invalid.")

    key_id, secret = parsed
    credential = db.scalar(select(NodeCredential).where(NodeCredential.key_id == key_id))
    supplied_hash = hash_node_secret(secret)
    expected_hash = credential.secret_hash if credential is not None else _DUMMY_SECRET_HASH
    valid_secret = hmac.compare_digest(expected_hash, supplied_hash)
    if credential is None or not valid_secret:
        raise NodeAuthError(401, "INVALID_CREDENTIAL", "The node API key is invalid.")

    now = utc_now()
    if credential.revoked_at is not None:
        raise NodeAuthError(401, "CREDENTIAL_REVOKED", "The node API key has been revoked.")
    if credential.expires_at is not None and _aware(credential.expires_at) <= now:
        raise NodeAuthError(401, "CREDENTIAL_EXPIRED", "The node API key has expired.")

    node = credential.franchise
    if not node.active:
        raise NodeAuthError(403, "NODE_INACTIVE", "This franchise node is inactive.")
    if apply_rate_limit:
        _check_rate_limit(key_id, now)

    credential.last_used_at = now
    node.last_seen_at = now
    return NodeAuthContext(node=node, credential=credential)
