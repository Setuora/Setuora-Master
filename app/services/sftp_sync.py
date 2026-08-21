from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET  # nosec B405

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FranchiseNode, SftpTallyImport, SftpTallyParty, utc_now

logger = logging.getLogger(__name__)

PARTY_PARENTS = {
    "sundry debtors": "DEBTOR",
    "sundry creditors": "CREDITOR",
}
SAFE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
FOLDERS = ("inbox", "outbox", "ack", "processed", "failed")


class SftpSyncError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedParty:
    name: str
    party_type: str
    parent: str
    opening_balance: str
    closing_balance: str
    mailing_name: str
    addresses: tuple[str, ...]
    pincode: str
    country: str
    state: str
    email: str
    phone: str
    mobile: str
    gstin: str


def normalize_franchise_code(code: str) -> str:
    normalized = code.strip().upper()
    if not SAFE_CODE.fullmatch(normalized):
        raise SftpSyncError("Franchise codes used for SFTP must contain only A-Z, 0-9, _ or -.")
    return normalized


def franchise_directory(root: Path, code: str) -> Path:
    return root / "franchises" / normalize_franchise_code(code)


def ensure_franchise_folders(root: Path, code: str) -> Path:
    directory = franchise_directory(root, code)
    for name in FOLDERS:
        (directory / name).mkdir(parents=True, exist_ok=True)
    return directory


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    expected = name.upper()
    return [child for child in element.iter() if _local_name(child.tag) == expected]


def _text(element: ET.Element, *names: str) -> str:
    for name in names:
        matches = _children(element, name)
        if matches and matches[0].text:
            return matches[0].text.strip()
    return ""


def _limited(value: str, length: int) -> str:
    return value.strip()[:length]


def parse_tally_parties(xml_bytes: bytes) -> list[ParsedParty]:
    try:
        root = safe_fromstring(xml_bytes)
    except (DefusedXmlException, ET.ParseError, ValueError) as exc:
        raise SftpSyncError("The uploaded file is not safe, valid XML.") from exc

    parties: list[ParsedParty] = []
    for ledger in _children(root, "LEDGER"):
        parent = _limited(_text(ledger, "PARENT"), 220)
        party_type = PARTY_PARENTS.get(parent.casefold())
        if not party_type:
            continue
        name = _limited(_text(ledger, "NAME") or ledger.attrib.get("NAME", ""), 220)
        if not name:
            raise SftpSyncError("A debtor/creditor LEDGER is missing its NAME.")
        addresses = tuple(
            _limited(item.text or "", 500)
            for item in _children(ledger, "ADDRESS")
            if (item.text or "").strip()
        )[:10]
        parties.append(
            ParsedParty(
                name=name,
                party_type=party_type,
                parent=parent,
                opening_balance=_limited(_text(ledger, "OPENINGBALANCE"), 80),
                closing_balance=_limited(_text(ledger, "CLOSINGBALANCE"), 80),
                mailing_name=_limited(_text(ledger, "MAILINGNAME"), 220),
                addresses=addresses,
                pincode=_limited(_text(ledger, "PINCODE"), 24),
                country=_limited(_text(ledger, "COUNTRYNAME", "COUNTRY"), 120),
                state=_limited(_text(ledger, "LEDSTATENAME", "STATENAME"), 120),
                email=_limited(_text(ledger, "EMAIL"), 320),
                phone=_limited(_text(ledger, "LEDGERPHONE", "PHONE"), 80),
                mobile=_limited(_text(ledger, "LEDGERMOBILE", "MOBILE"), 80),
                gstin=_limited(_text(ledger, "PARTYGSTIN", "GSTIN"), 32),
            )
        )
    if not parties:
        raise SftpSyncError("No ledgers under Sundry Debtors or Sundry Creditors were found.")
    return parties


def _add_text(parent: ET.Element, tag: str, value: str) -> None:
    if value:
        ET.SubElement(parent, tag).text = value


def build_tally_import_xml(parties: list[SftpTallyParty]) -> bytes:
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"
    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(request_desc, "REPORTNAME").text = "All Masters"
    request_data = ET.SubElement(import_data, "REQUESTDATA")

    for party in parties:
        message = ET.SubElement(request_data, "TALLYMESSAGE")
        ledger = ET.SubElement(message, "LEDGER", {"NAME": party.name, "ACTION": "Create"})
        _add_text(ledger, "NAME", party.name)
        _add_text(ledger, "PARENT", party.parent)
        _add_text(ledger, "OPENINGBALANCE", party.opening_balance)
        _add_text(ledger, "MAILINGNAME", party.mailing_name)
        try:
            addresses = json.loads(party.addresses_json)
        except (TypeError, ValueError):
            addresses = []
        if addresses:
            address_list = ET.SubElement(ledger, "ADDRESS.LIST", {"TYPE": "String"})
            for address in addresses:
                _add_text(address_list, "ADDRESS", str(address))
        _add_text(ledger, "PINCODE", party.pincode)
        _add_text(ledger, "COUNTRYNAME", party.country)
        _add_text(ledger, "LEDSTATENAME", party.state)
        _add_text(ledger, "EMAIL", party.email)
        _add_text(ledger, "LEDGERPHONE", party.phone)
        _add_text(ledger, "LEDGERMOBILE", party.mobile)
        _add_text(ledger, "PARTYGSTIN", party.gstin)

    ET.indent(envelope, space="  ")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _safe_destination(directory: Path, filename: str) -> Path:
    destination = directory / filename
    if not destination.exists():
        return destination
    stem, suffix = destination.stem, destination.suffix
    counter = 2
    while (candidate := directory / f"{stem}-{counter}{suffix}").exists():
        counter += 1
    return candidate


def _archive(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = _safe_destination(directory, source.name)
    return Path(shutil.move(str(source), str(destination)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_failed_import(
    db: Session,
    node: FranchiseNode,
    source: Path,
    digest: str,
    error: str,
) -> None:
    db.add(
        SftpTallyImport(
            franchise_id=node.id,
            original_filename=source.name,
            file_sha256=digest,
            status="FAILED",
            error=error[:2000],
            completed_at=utc_now(),
        )
    )
    db.commit()


def _acknowledge_pending(directory: Path) -> bool:
    pending = sorted((directory / "outbox").glob("*.xml"))
    for outbound in pending:
        acknowledgement = directory / "ack" / f"{outbound.stem}.ack"
        if not acknowledgement.is_file():
            continue
        _archive(outbound, directory / "processed" / "outbound")
        _archive(acknowledgement, directory / "processed" / "ack")
    return any((directory / "outbox").glob("*.xml"))


def _write_outbound(directory: Path, franchise_code: str, xml_bytes: bytes) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"setuora-{franchise_code}-debtors-creditors-{stamp}.xml"
    temporary = directory / "outbox" / f".{filename}.tmp"
    destination = directory / "outbox" / filename
    temporary.write_bytes(xml_bytes)
    os.replace(temporary, destination)
    return destination


def _upsert_parties(
    db: Session,
    node: FranchiseNode,
    parsed: list[ParsedParty],
    file_sha256: str,
) -> None:
    for item in parsed:
        name_key = item.name.casefold()
        party = db.scalar(select(SftpTallyParty).where(SftpTallyParty.name_key == name_key))
        if party is None:
            party = SftpTallyParty(name_key=name_key, name=item.name)
            db.add(party)
        party.name = item.name
        party.party_type = item.party_type
        party.parent = item.parent
        party.opening_balance = item.opening_balance
        party.closing_balance = item.closing_balance
        party.mailing_name = item.mailing_name
        party.addresses_json = json.dumps(item.addresses, ensure_ascii=False)
        party.pincode = item.pincode
        party.country = item.country
        party.state = item.state
        party.email = item.email
        party.phone = item.phone
        party.mobile = item.mobile
        party.gstin = item.gstin
        party.source_franchise_id = node.id
        party.source_file_sha256 = file_sha256
        party.updated_at = utc_now()


def process_franchise_once(db: Session, node: FranchiseNode, root: Path) -> str:
    settings = get_settings()
    directory = ensure_franchise_folders(root, node.code)
    if _acknowledge_pending(directory):
        return "waiting_for_tally_import"

    now = datetime.now(UTC).timestamp()
    candidates = sorted((directory / "inbox").glob("*.xml"), key=lambda path: path.stat().st_mtime)
    for source in candidates:
        stat = source.stat()
        if now - stat.st_mtime < settings.sftp_file_settle_seconds:
            continue
        digest = _sha256_file(source)
        existing = db.scalar(
            select(SftpTallyImport).where(
                SftpTallyImport.franchise_id == node.id,
                SftpTallyImport.file_sha256 == digest,
            )
        )
        if existing is not None:
            _archive(source, directory / "processed" / "duplicate")
            return "duplicate"
        if stat.st_size > settings.sftp_max_xml_bytes:
            _record_failed_import(
                db,
                node,
                source,
                digest,
                f"The uploaded XML exceeds the {settings.sftp_max_xml_bytes}-byte limit.",
            )
            _archive(source, directory / "failed")
            return "file_too_large"
        raw = source.read_bytes()

        import_record = SftpTallyImport(
            franchise_id=node.id,
            original_filename=source.name,
            file_sha256=digest,
            status="PROCESSING",
        )
        db.add(import_record)
        outbound_path: Path | None = None
        try:
            parsed = parse_tally_parties(raw)
            _upsert_parties(db, node, parsed, digest)
            db.flush()
            all_parties = db.scalars(select(SftpTallyParty).order_by(SftpTallyParty.name_key)).all()
            outbound = build_tally_import_xml(list(all_parties))
            outbound_path = _write_outbound(directory, node.code, outbound)
            import_record.status = "IMPORTED"
            import_record.imported_count = len(parsed)
            import_record.completed_at = utc_now()
            node.last_seen_at = utc_now()
            db.commit()
            try:
                _archive(source, directory / "processed" / "inbound")
            except OSError:
                logger.exception("Could not archive accepted SFTP file %s", source)
            return "imported"
        except (OSError, SQLAlchemyError, SftpSyncError) as exc:
            db.rollback()
            if outbound_path is not None:
                outbound_path.unlink(missing_ok=True)
            _record_failed_import(db, node, source, digest, str(exc))
            _archive(source, directory / "failed")
            return "failed"
    return "idle"


def run_sftp_sync_cycle(db: Session, root: Path | None = None) -> dict[str, str]:
    exchange_root = root or Path(get_settings().sftp_exchange_root)
    results: dict[str, str] = {}
    nodes = db.scalars(
        select(FranchiseNode).where(FranchiseNode.active.is_(True)).order_by(FranchiseNode.code)
    ).all()
    for node in nodes:
        try:
            results[node.code] = process_franchise_once(db, node, exchange_root)
        except (OSError, SftpSyncError):
            db.rollback()
            logger.exception("SFTP Tally sync failed for franchise %s", node.code)
            results[node.code] = "failed"
    return results
