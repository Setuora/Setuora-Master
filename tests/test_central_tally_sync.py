from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from app.models import Batch, BatchStatus, Setting
from app.network_schemas import EventBatchRequest
from app.services import tally as tally_service
from app.services.network_ingest import NetworkIngestError, ingest_events
from app.services.node_auth import create_franchise_node
from app.services.tally import (
    TallyResult,
    TallySyncError,
    build_voucher_xml,
    reconcile_batch,
    sync_batch,
)
from app.services.voucher import calculate_voucher_summary
from tests.test_network_sync import event, initial_snapshot, item
from tests.test_tally import VALID_SETTINGS


def test_network_sale_preserves_discount_and_franchise_godown(db_session):
    node = create_franchise_node(db_session, code="F1", name="F1", location="F1")
    node.tally_godown_name = "Franchise One"
    stock = item("F1-1")
    stock["sales_discount_rate"] = 10
    sale = {**stock, "status": "SOLD", "rate": 500}
    request = EventBatchRequest.model_validate(
        {
            "events": [
                initial_snapshot(node, [stock]),
                event(2, "SALE", [sale], party_name="Customer"),
            ]
        }
    )
    result = ingest_events(db_session, node, request)
    db_session.commit()
    batch = db_session.get(Batch, result[-1]["result"]["batch_id"])
    # Later configuration or product edits must not change a queued transaction.
    node.tally_godown_name = "Moved Franchise"
    batch.items[0].serial.product.sales_discount_rate = 50
    db_session.commit()

    assert calculate_voucher_summary(batch).final_value == Decimal("450.00")
    xml = ET.fromstring(build_voucher_xml(batch, VALID_SETTINGS))
    assert xml.findtext(".//DISCOUNT") == "10.00"
    assert xml.findtext(".//GODOWNNAME") == "Franchise One"


def test_zero_network_sale_price_stays_zero(db_session):
    node = create_franchise_node(db_session, code="F2", name="F2", location="F2")
    stock = item("F2-1")
    request = EventBatchRequest.model_validate(
        {
            "events": [
                initial_snapshot(node, [stock]),
                event(2, "SALE", [{**stock, "rate": 0}], party_name="Customer"),
            ]
        }
    )
    result = ingest_events(db_session, node, request)
    db_session.commit()
    batch = db_session.get(Batch, result[-1]["result"]["batch_id"])
    assert calculate_voucher_summary(batch).final_value == Decimal("0.00")


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_network_event_rejects_nonfinite_rates(value):
    with pytest.raises(ValueError):
        EventBatchRequest.model_validate(
            {
                "events": [
                    event(1, "STOCK_SNAPSHOT", [{**item("F3-1"), "rate": value}]),
                ]
            }
        )


@pytest.mark.parametrize(
    "changed", [{"gst_rate": 5}, {"tally_stock_item_name": "Other stock item"}]
)
def test_network_sale_rejects_changed_accounting_product_identity(db_session, changed):
    node = create_franchise_node(db_session, code="F5", name="F5", location="F5")
    stock = item("F5-1")
    ingest_events(
        db_session,
        node,
        EventBatchRequest.model_validate({"events": [initial_snapshot(node, [stock])]}),
    )
    db_session.commit()
    with pytest.raises(NetworkIngestError) as caught:
        ingest_events(
            db_session,
            node,
            EventBatchRequest.model_validate(
                {"events": [event(2, "SALE", [{**stock, **changed}], party_name="Customer")]}
            ),
        )
    assert caught.value.code == "PRODUCT_IDENTITY_CONFLICT"
    db_session.rollback()
    assert node.last_sequence == 1


def test_timeout_pauses_central_queue_until_verified_absent(db_session, monkeypatch):
    node = create_franchise_node(db_session, code="F4", name="F4", location="F4")
    stock = item("F4-1")
    result = ingest_events(
        db_session,
        node,
        EventBatchRequest.model_validate(
            {
                "events": [
                    initial_snapshot(node, [stock]),
                    event(2, "SALE", [stock], party_name="Customer"),
                    event(3, "SALES_RETURN", [stock], party_name="Customer"),
                ]
            }
        ),
    )
    db_session.add_all(
        Setting(key=k, value=v)
        for k, v in {
            **VALID_SETTINGS,
            "tally_enabled": "true",
            "tally_host": "localhost",
            "tally_port": "9000",
        }.items()
    )
    db_session.commit()
    sale = db_session.get(Batch, result[1]["result"]["batch_id"])
    returned = db_session.get(Batch, result[2]["result"]["batch_id"])
    posted = []

    def timeout(xml, _settings):
        posted.append(xml)
        raise TallySyncError("Unknown outcome", retryable=False, outcome_unknown=True)

    monkeypatch.setattr(tally_service, "post_to_tally", timeout)
    sync_batch(db_session, sale)
    assert sale.status == BatchStatus.REVIEW_REQUIRED.value
    sync_batch(db_session, returned)
    assert len(posted) == 1
    assert returned.status == BatchStatus.PENDING_SYNC.value

    reconcile_batch(db_session, sale, imported=False, actor="admin")
    assert sale.status == BatchStatus.PENDING_SYNC.value
    sale.items[0].rate = 900
    db_session.commit()

    def accepted(xml, _settings):
        posted.append(xml)
        return TallyResult(xml, "<RESPONSE><CREATED>1</CREATED></RESPONSE>", "CREATED=1")

    monkeypatch.setattr(tally_service, "post_to_tally", accepted)
    sync_batch(db_session, sale)
    assert sale.status == BatchStatus.SYNCED.value
    assert posted[0] == posted[1]
    sync_batch(db_session, returned)
    assert returned.status == BatchStatus.SYNCED.value
