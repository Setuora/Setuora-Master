import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("SETUORA_APP_MODE", "master")
os.environ.setdefault("SFTP_SYNC_ENABLED", "false")
# Keep the Sync Monitor snapshot that app startup writes out of the project folder.
os.environ.setdefault("SETUORA_STATUS_DIR", tempfile.mkdtemp(prefix="setuora-status-"))

from app.database import Base


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
