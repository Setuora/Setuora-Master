"""Run Lite's actual connection UI in a fresh process with a persistent database."""

import json
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path.cwd()))

from app.routers.lite_sync import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import SESSION_COOKIE
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import MasterOutboxEvent, Setting, User
from app.security import create_session_token
from app.services import master_sync


def main():
    instruction = json.loads(sys.stdin.readline())
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User))
        if user is None:
            user = User(username="setup-admin", password_hash="unused", role="admin")
            db.add(user)
            db.commit()
        user_id = user.id

    requests = []

    def master_opener(request, **_kwargs):
        expected_key = instruction["expected_credential"]
        assert request.get_header("Authorization") == f"Bearer {expected_key}"
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "warehouse.example.test"
        requests.append(request.full_url)
        print(
            json.dumps(
                {
                    "kind": "master_request",
                    "method": request.method,
                    "path": parsed.path + ("?" + parsed.query if parsed.query else ""),
                    "body": request.data.decode() if request.data else None,
                }
            ),
            flush=True,
        )
        result = json.loads(sys.stdin.readline())
        response = BytesIO(result["body"].encode())
        response.status = result["status"]
        return response

    master_sync._master_urlopen = master_opener
    app = FastAPI()
    app.state.app_mode = "lite"
    app.include_router(router)
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, create_session_token(user_id))
        action = instruction["action"]
        if action == "connect":
            response = client.post(
                "/master-connection/connect",
                data={"setup_details": json.dumps(instruction["bundle"])},
            )
        elif action == "initialize":
            response = client.post("/master-connection/initialize")
        elif action == "inspect":
            response = client.get("/master-connection")
        else:
            raise ValueError(action)
        credential = instruction.get("expected_credential", "")
        assert not credential or credential not in response.text
        assert not credential or credential not in response.headers.get("location", "")

    settings = get_settings()
    with SessionLocal() as db:
        rows = db.scalars(select(MasterOutboxEvent).order_by(MasterOutboxEvent.id)).all()
        node = db.get(Setting, "_master_connection_node_id")
        result = {
            "status": response.status_code,
            "location": response.headers.get("location", ""),
            "requests": requests,
            "franchise_code": settings.franchise_code,
            "master_url": settings.master_url,
            "credential_matches": settings.master_api_key == instruction.get("expected_credential"),
            "enabled": settings.master_sync_enabled,
            "bound_node_id": node.value if node else None,
            "events": [json.loads(row.payload_json)["events"][0] for row in rows],
        }
    print(json.dumps({"kind": "result", "data": result}), flush=True)


if __name__ == "__main__":
    main()
