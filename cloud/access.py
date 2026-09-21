"""Strict Google identity and fresh persistent membership for every action.

The pure allowlist helper remains for setup/offline tests. Deployed access is
resolved through Neon; identity never comes from query parameters.
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
    status: str = ""

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


def verified_identity(claims):
    """Use the same strict OIDC validation for registration and every action."""
    email=normalized_email(claims.get('email')) if isinstance(claims,Mapping) else ''
    checked=authorize(claims,{'viewer_emails':[email]})
    return (email,claims['sub']) if checked.allowed else None


def current_access(claims, config, *, register=False, store_factory=None):
    identity=verified_identity(claims)
    if not identity:
        return Access()
    # Offline/setup mode retains the legacy allowlist. The deployed application
    # always checks persistent membership; database failure never grants access.
    if not config.get('cloud',{}).get('database_url'):
        return authorize(claims,config.get('access',{}))
    from .members import MemberStore
    store=(store_factory or MemberStore)(config)
    email,subject=identity
    member=store.find(email,subject)
    if register and (member is None or member.get('subject') is None):
        member=store.register(email,subject)
    if not member or member.get('subject')!=subject:
        return Access(email=email)
    if member['status']!='active':
        return Access(email=email,status='blocked')
    return Access(role=member['role'],email=member['email'],status='active')


def current_admin(claims, config):
    access=current_access(claims,config)
    if access.role!='admin':
        raise PermissionError('Это действие доступно администратору.')
    return access
