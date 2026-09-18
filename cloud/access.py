"""Authorization for the first deployment. Identity comes ONLY from st.user/OIDC.

Invitations are an explicit email allowlist in server-side Secrets until the
shared membership store is implemented. Never accept identity from query params.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import re


ROLE_LABELS = {"admin": "Администратор", "editor": "Редактор", "viewer": "Читатель"}
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def normalized_email(value):
    if not isinstance(value, str):
        return ""
    email = value.strip().lower()
    # Do not merge dots or +aliases: invitations must match the verified claim.
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return ""
    return email


@dataclass(frozen=True)
class Access:
    role: str = ""
    email: str = ""

    @property
    def allowed(self):
        return self.role in ROLE_LABELS


def authorize(claims, invitations):
    if not isinstance(claims, Mapping) or not isinstance(invitations, Mapping):
        return Access()
    if claims.get("is_logged_in") is not True or claims.get("email_verified") is not True:
        return Access()
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or issuer not in GOOGLE_ISSUERS:
        return Access()
    if not isinstance(subject, str) or not subject.strip():
        return Access()
    email = normalized_email(claims.get("email"))
    if not email:
        return Access()
    for role in ROLE_LABELS:
        configured = invitations.get(role + "_emails", [])
        if not isinstance(configured, (tuple, list)):
            continue
        if email in {normalized_email(value) for value in configured}:
            return Access(role=role, email=email)
    return Access()


def require_admin(claims, invitations):
    access = authorize(claims, invitations)
    if access.role != "admin":
        raise PermissionError("Это действие доступно администратору.")
    return access
