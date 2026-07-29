from datetime import date, datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NetworkEventType(str, Enum):
    STOCK_SNAPSHOT = "STOCK_SNAPSHOT"
    PURCHASE = "PURCHASE"
    RECEIVE = "RECEIVE"
    SALE = "SALE"
    SALES_RETURN = "SALES_RETURN"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    ISSUE = "ISSUE"
    AUDIT = "AUDIT"
    TRANSFER_DISPATCHED = "TRANSFER_DISPATCHED"
    TRANSFER_RECEIVED = "TRANSFER_RECEIVED"
    HEARTBEAT = "HEARTBEAT"


class NetworkEventItem(BaseModel):
    """The complete product/QR snapshot carried with every stock event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    serial_number: str = Field(min_length=1, max_length=140)
    product_code: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=180)
    tally_stock_item_name: str = Field(min_length=1, max_length=180)
    hsn: str = Field(max_length=40)
    gst_rate: float = Field(ge=0, le=100)
    unit: str = Field(min_length=1, max_length=40)
    rate: float = Field(ge=0)
    status: str = Field(min_length=1, max_length=40)
    product_batch_number: str | None = Field(default=None, max_length=80)
    mfg_date: date | None = None
    expiry_date: date | None = None
    warehouse: str | None = Field(default=None, max_length=80)

    @field_validator("product_code", "status")
    @classmethod
    def uppercase_identifiers(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_dates(self):
        if self.mfg_date and self.expiry_date and self.expiry_date < self.mfg_date:
            raise ValueError("expiry_date cannot be before mfg_date")
        return self


class NetworkEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: UUID
    sequence: int = Field(ge=1)
    schema_version: Literal[1] = 1
    type: NetworkEventType
    occurred_at: datetime
    reference: str | None = Field(default=None, max_length=180)
    actor: str | None = Field(default=None, max_length=120)
    items: list[NetworkEventItem] = Field(default_factory=list, max_length=5000)

    # Financial/Tally metadata. These fields deliberately mirror the existing
    # Batch shape so queued batches can use the current Tally XML builder.
    party_name: str | None = Field(default=None, max_length=180)
    party_state: str | None = Field(default=None, max_length=80)
    party_gst_registration_type: str | None = Field(default=None, max_length=40)
    party_gst_name: str | None = Field(default=None, max_length=180)
    party_gstin: str | None = Field(default=None, max_length=20)
    gst_treatment: str | None = Field(default=None, max_length=40)
    gst_cgst_rate: float | None = Field(default=None, ge=0, le=100)
    gst_sgst_rate: float | None = Field(default=None, ge=0, le=100)
    gst_igst_rate: float | None = Field(default=None, ge=0, le=100)
    reason_code: str | None = Field(default=None, max_length=80)

    # Transfer metadata.
    destination_franchise_code: str | None = Field(default=None, max_length=40)
    transfer_id: UUID | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("destination_franchise_code")
    @classmethod
    def uppercase_destination(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("party_gstin")
    @classmethod
    def uppercase_gstin(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def validate_event_shape(self):
        if self.type == NetworkEventType.HEARTBEAT:
            if self.items:
                raise ValueError("HEARTBEAT cannot contain stock items")
            return self

        if not self.items:
            raise ValueError(f"{self.type.value} requires at least one item")

        serials = [item.serial_number for item in self.items]
        if len(serials) != len(set(serials)):
            raise ValueError("an event cannot contain the same serial_number twice")

        if self.type == NetworkEventType.TRANSFER_DISPATCHED:
            if not self.destination_franchise_code:
                raise ValueError("TRANSFER_DISPATCHED requires destination_franchise_code")
            if not self.reference:
                raise ValueError("TRANSFER_DISPATCHED requires reference")

        if self.type == NetworkEventType.TRANSFER_RECEIVED and not self.transfer_id:
            raise ValueError("TRANSFER_RECEIVED requires transfer_id")
        return self


class EventBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[NetworkEventV1] = Field(min_length=1, max_length=100)


class CommandAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: Literal[True] = True
