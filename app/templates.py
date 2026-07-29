from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from app.database import SessionLocal
from app.models import has_any_role
from app.services.access_control import configured_role_has_access, role_has_access
from app.services.report_format import report_date

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["report_date"] = report_date
templates.env.filters["report_date"] = report_date


def csp_nonce(request=None) -> str:
    if isinstance(request, Undefined):
        return ""
    return getattr(getattr(request, "state", None), "csp_nonce", "")


templates.env.globals["csp_nonce"] = csp_nonce


def role_can(subject, access_key: str) -> bool:
    if not subject:
        return False
    role = getattr(subject, "role", subject)
    access_config = getattr(subject, "_access_config", None)
    if access_config is not None:
        return configured_role_has_access(access_config, role, access_key)
    with SessionLocal() as db:
        return role_has_access(db, role, access_key)


templates.env.globals["role_can"] = role_can


def has_role(subject, *roles) -> bool:
    if not subject:
        return False
    role = getattr(subject, "role", subject)
    return has_any_role(role, roles)


templates.env.globals["has_role"] = has_role
