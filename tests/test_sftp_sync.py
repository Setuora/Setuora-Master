from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

from app.models import FranchiseNode, SftpTallyImport, SftpTallyParty
from app.services import sftp_sync


def tally_xml(name: str = "Customer One", parent: str = "Sundry Debtors") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ENVELOPE><BODY><DATA><COLLECTION>
  <LEDGER NAME="{name}">
    <NAME>{name}</NAME><PARENT>{parent}</PARENT>
    <OPENINGBALANCE>1250.00</OPENINGBALANCE>
    <CLOSINGBALANCE>1500.00</CLOSINGBALANCE>
    <ADDRESS.LIST><ADDRESS>First line</ADDRESS><ADDRESS>Bengaluru</ADDRESS></ADDRESS.LIST>
    <PINCODE>560001</PINCODE><COUNTRYNAME>India</COUNTRYNAME>
    <LEDSTATENAME>Karnataka</LEDSTATENAME><EMAIL>party@example.com</EMAIL>
    <LEDGERMOBILE>9999999999</LEDGERMOBILE><PARTYGSTIN>29ABCDE1234F1Z5</PARTYGSTIN>
  </LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>""".encode()


@pytest.fixture()
def sync_settings(monkeypatch):
    settings = SimpleNamespace(sftp_file_settle_seconds=0, sftp_max_xml_bytes=1024 * 1024)
    monkeypatch.setattr(sftp_sync, "get_settings", lambda: settings)
    return settings


def add_node(db_session, code: str = "BLR-01") -> FranchiseNode:
    node = FranchiseNode(code=code, name=f"{code} FRANCHISE", location="BENGALURU")
    db_session.add(node)
    db_session.commit()
    db_session.refresh(node)
    return node


def test_imports_parties_and_publishes_tally_xml(db_session, tmp_path, sync_settings):
    node = add_node(db_session)
    directory = sftp_sync.ensure_franchise_folders(tmp_path, node.code)
    (directory / "inbox" / "masters.xml").write_bytes(tally_xml())

    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "imported"

    party = db_session.scalar(select(SftpTallyParty))
    assert party is not None
    assert party.name == "Customer One"
    assert party.party_type == "DEBTOR"
    assert party.source_franchise_id == node.id
    assert party.closing_balance == "1500.00"
    assert not (directory / "inbox" / "masters.xml").exists()
    assert (directory / "processed" / "inbound" / "masters.xml").is_file()

    outbound = next((directory / "outbox").glob("*.xml"))
    root = ET.fromstring(outbound.read_bytes())
    ledger = root.find("./BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE/LEDGER")
    assert ledger is not None
    assert ledger.attrib == {"NAME": "Customer One", "ACTION": "Create"}
    assert ledger.findtext("PARENT") == "Sundry Debtors"
    assert ledger.find("CLOSINGBALANCE") is None


def test_pending_outbound_blocks_next_upload_until_ack(db_session, tmp_path, sync_settings):
    node = add_node(db_session)
    directory = sftp_sync.ensure_franchise_folders(tmp_path, node.code)
    (directory / "inbox" / "first.xml").write_bytes(tally_xml())
    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "imported"

    outbound = next((directory / "outbox").glob("*.xml"))
    (directory / "inbox" / "second.xml").write_bytes(tally_xml("Supplier Two", "Sundry Creditors"))
    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == (
        "waiting_for_tally_import"
    )
    assert (directory / "inbox" / "second.xml").is_file()

    (directory / "ack" / f"{outbound.stem}.ack").write_bytes(b"")
    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "imported"
    parties = db_session.scalars(select(SftpTallyParty).order_by(SftpTallyParty.name)).all()
    assert [(party.name, party.party_type) for party in parties] == [
        ("Customer One", "DEBTOR"),
        ("Supplier Two", "CREDITOR"),
    ]
    assert (directory / "processed" / "outbound" / outbound.name).is_file()


def test_duplicate_upload_is_idempotent(db_session, tmp_path, sync_settings):
    node = add_node(db_session)
    directory = sftp_sync.ensure_franchise_folders(tmp_path, node.code)
    payload = tally_xml()
    (directory / "inbox" / "first.xml").write_bytes(payload)
    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "imported"
    outbound = next((directory / "outbox").glob("*.xml"))
    (directory / "ack" / f"{outbound.stem}.ack").write_bytes(b"")
    (directory / "inbox" / "again.xml").write_bytes(payload)

    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "duplicate"
    assert len(db_session.scalars(select(SftpTallyParty)).all()) == 1
    assert len(db_session.scalars(select(SftpTallyImport)).all()) == 1
    assert (directory / "processed" / "duplicate" / "again.xml").is_file()


def test_invalid_or_unsafe_xml_is_rejected(db_session, tmp_path, sync_settings):
    node = add_node(db_session)
    directory = sftp_sync.ensure_franchise_folders(tmp_path, node.code)
    unsafe = b'<!DOCTYPE x [<!ENTITY file SYSTEM "file:///etc/passwd">]><ENVELOPE>&file;</ENVELOPE>'
    (directory / "inbox" / "unsafe.xml").write_bytes(unsafe)

    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "failed"
    record = db_session.scalar(select(SftpTallyImport))
    assert record is not None
    assert record.status == "FAILED"
    assert "safe, valid XML" in record.error
    assert (directory / "failed" / "unsafe.xml").is_file()


def test_oversized_xml_is_rejected_and_audited(db_session, tmp_path, sync_settings):
    sync_settings.sftp_max_xml_bytes = 10
    node = add_node(db_session)
    directory = sftp_sync.ensure_franchise_folders(tmp_path, node.code)
    (directory / "inbox" / "large.xml").write_bytes(tally_xml())

    assert sftp_sync.process_franchise_once(db_session, node, tmp_path) == "file_too_large"
    record = db_session.scalar(select(SftpTallyImport))
    assert record is not None
    assert record.status == "FAILED"
    assert "10-byte limit" in record.error
    assert (directory / "failed" / "large.xml").is_file()


@pytest.mark.parametrize("code", ["../BAD", "HAS SPACE", "TOO-LONG-FRANCHISE-CODE"])
def test_franchise_folder_rejects_unsafe_windows_account_codes(tmp_path, code):
    with pytest.raises(sftp_sync.SftpSyncError):
        sftp_sync.ensure_franchise_folders(tmp_path, code)
