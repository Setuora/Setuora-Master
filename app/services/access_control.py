from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Role, normalize_role_values
from app.services.settings import get_setting, update_settings

SETTING_KEY = "role_access_config"
DENY_VALUES = {"hidden", "no"}


@dataclass(frozen=True)
class RoleColumn:
    key: str
    label: str


@dataclass(frozen=True)
class AccessOption:
    value: str
    label: str


@dataclass(frozen=True)
class AccessCell:
    value: str
    label: str
    tone: str
    options: list[AccessOption]


@dataclass(frozen=True)
class AccessRowDefinition:
    key: str
    item: str
    context: str
    defaults: dict[str, str]
    options: list[AccessOption]
    note: str = ""


@dataclass(frozen=True)
class AccessRow:
    key: str
    item: str
    context: str
    cells: list[AccessCell]
    note: str = ""


@dataclass(frozen=True)
class AccessSectionDefinition:
    title: str
    context_heading: str
    rows: list[AccessRowDefinition]


@dataclass(frozen=True)
class AccessSection:
    title: str
    context_heading: str
    rows: list[AccessRow]


ROLE_COLUMNS = [
    RoleColumn(Role.SUPER_ADMIN.value, "Super admin"),
    RoleColumn(Role.ADMIN.value, "Admin"),
    RoleColumn(Role.DIRECTORS.value, "Directors"),
]

ACTION_OPTIONS = [
    AccessOption("edit", "Edit"),
    AccessOption("yes", "Yes"),
    AccessOption("no", "No"),
]
DATA_OPTIONS = [
    AccessOption("view", "View"),
    AccessOption("no", "No"),
]

CELL_META = {
    "edit": ("Edit", "synced"),
    "yes": ("Yes", "synced"),
    "view": ("View", "generated"),
    "no": ("No", "failed"),
}


def _defaults(
    allowed_value: str,
    *,
    admin: bool = True,
    directors: bool = False,
) -> dict[str, str]:
    return {
        Role.SUPER_ADMIN.value: allowed_value,
        Role.ADMIN.value: allowed_value if admin else "no",
        Role.DIRECTORS.value: allowed_value if directors else "no",
    }


def access_section_definitions() -> list[AccessSectionDefinition]:
    return [
        AccessSectionDefinition(
            title="Master administration",
            context_heading="Capability",
            rows=[
                AccessRowDefinition(
                    "settings_edit",
                    "Tally settings",
                    "Configure companies, ledgers, gateway, and sync",
                    _defaults("edit"),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition(
                    "tally_check_edit",
                    "Tally Check",
                    "Refresh and confirm Tally masters",
                    _defaults("edit"),
                    ACTION_OPTIONS,
                ),
                AccessRowDefinition(
                    "users_manage",
                    "Users",
                    "Create, disable, and scope Master accounts",
                    _defaults("edit"),
                    ACTION_OPTIONS,
                    "Only super admins can create another super admin or reset passwords.",
                ),
                AccessRowDefinition(
                    "role_access_edit",
                    "Role access",
                    "Edit this permission matrix",
                    _defaults("edit", admin=False),
                    ACTION_OPTIONS,
                    "Always restricted to super admins.",
                ),
            ],
        ),
        AccessSectionDefinition(
            title="Backup access",
            context_heading="Capability",
            rows=[
                AccessRowDefinition(
                    "backup_data",
                    "Maintenance",
                    "View backup status and recovery guidance",
                    _defaults("view"),
                    DATA_OPTIONS,
                ),
                AccessRowDefinition(
                    "backup_download",
                    "Download backup",
                    "Create and download a verified SQLite backup",
                    _defaults("yes"),
                    ACTION_OPTIONS,
                ),
            ],
        ),
    ]


def _definitions_by_key() -> dict[str, AccessRowDefinition]:
    return {row.key: row for section in access_section_definitions() for row in section.rows}


def default_role_access_config() -> dict[str, dict[str, str]]:
    return {key: row.defaults.copy() for key, row in _definitions_by_key().items()}


def _valid_option(row: AccessRowDefinition, value: str) -> bool:
    return value in {option.value for option in row.options}


def normalize_role_access_config(
    raw_config: dict | None,
) -> dict[str, dict[str, str]]:
    definitions = _definitions_by_key()
    config = default_role_access_config()
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    for row_key, row_values in raw_config.items():
        row = definitions.get(row_key)
        if not row or not isinstance(row_values, dict):
            continue
        for role in ROLE_COLUMNS:
            if role.key == Role.SUPER_ADMIN.value:
                continue
            value = str(row_values.get(role.key, "")).strip()
            if _valid_option(row, value):
                config[row_key][role.key] = value

    for row_key, row in definitions.items():
        config[row_key][Role.SUPER_ADMIN.value] = row.defaults[Role.SUPER_ADMIN.value]
        if row_key == "role_access_edit":
            config[row_key][Role.ADMIN.value] = "no"
            config[row_key][Role.DIRECTORS.value] = "no"
    return config


def get_role_access_config(
    db: Session,
) -> dict[str, dict[str, str]]:
    raw = get_setting(db, SETTING_KEY, "")
    if not raw:
        return default_role_access_config()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return default_role_access_config()
    return normalize_role_access_config(parsed)


def save_role_access_config(
    db: Session,
    submitted: dict[str, dict[str, str]],
    *,
    commit: bool = True,
) -> None:
    config = get_role_access_config(db)
    for row_key, row_values in submitted.items():
        config.setdefault(row_key, {}).update(row_values)
    normalized = normalize_role_access_config(config)
    update_settings(
        db,
        {SETTING_KEY: json.dumps(normalized, sort_keys=True)},
        commit=commit,
    )


def config_from_form(
    form_items,
) -> dict[str, dict[str, str]]:
    definitions = _definitions_by_key()
    config: dict[str, dict[str, str]] = {}
    for key, value in form_items:
        if not key.startswith("access__"):
            continue
        parts = key.split("__", 2)
        if len(parts) != 3:
            continue
        _, row_key, role_key = parts
        row = definitions.get(row_key)
        if (
            not row
            or role_key == Role.SUPER_ADMIN.value
            or role_key not in {role.key for role in ROLE_COLUMNS}
        ):
            continue
        normalized = str(value).strip()
        if _valid_option(row, normalized):
            config.setdefault(row_key, {})[role_key] = normalized
    return config


def _cell(
    value: str,
    options: list[AccessOption],
) -> AccessCell:
    label, tone = CELL_META.get(value, (value.title(), "generated"))
    return AccessCell(
        value=value,
        label=label,
        tone=tone,
        options=options,
    )


def role_access_sections(
    db: Session | None = None,
) -> list[AccessSection]:
    config = get_role_access_config(db) if db is not None else default_role_access_config()
    sections: list[AccessSection] = []
    for section in access_section_definitions():
        rows = []
        for row in section.rows:
            row_config = config.get(row.key, row.defaults)
            rows.append(
                AccessRow(
                    key=row.key,
                    item=row.item,
                    context=row.context,
                    note=row.note,
                    cells=[
                        _cell(
                            row_config.get(role.key, row.defaults[role.key]),
                            row.options,
                        )
                        for role in ROLE_COLUMNS
                    ],
                )
            )
        sections.append(
            AccessSection(
                section.title,
                section.context_heading,
                rows,
            )
        )
    return sections


def _configured_access_values(
    config: dict[str, dict[str, str]],
    role: Role | str,
    access_key: str,
) -> list[str]:
    values = []
    for role_value in normalize_role_values(role):
        if role_value == Role.SUPER_ADMIN.value:
            values.append(
                config.get(access_key, {}).get(
                    role_value,
                    "edit",
                )
            )
        else:
            values.append(
                config.get(access_key, {}).get(
                    role_value,
                    "no",
                )
            )
    return values


def configured_role_has_access(
    config: dict[str, dict[str, str]],
    role: Role | str,
    access_key: str,
    allowed_values: set[str] | None = None,
) -> bool:
    values = _configured_access_values(config, role, access_key)
    if allowed_values is not None:
        return any(value not in DENY_VALUES and value in allowed_values for value in values)
    return any(value not in DENY_VALUES for value in values)


def role_has_access(
    db: Session,
    role: Role | str,
    access_key: str,
    allowed_values: set[str] | None = None,
) -> bool:
    return configured_role_has_access(
        get_role_access_config(db),
        role,
        access_key,
        allowed_values,
    )
