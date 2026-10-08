"""OpenStack access for deferred Glance image exports.

Admission uses only the caller's original validated token. Deferred worker I/O
uses only a bounded Keystone Trust created by that caller: trustor = requester,
trustee = this service identity (resolved by authenticating to its own service
project), project = the admitted project, impersonation on, finite expiry and
least delegated roles. The service password is never scoped to a tenant
project, and nothing here falls back to it when a delegation is unusable.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import openstack
from keystoneauth1 import exceptions as ks_exceptions
from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3
from keystoneauth1.identity.access import AccessInfoPlugin

from palimpsest_hub.config import FORBIDDEN_DELEGATED_ROLES, get_settings, validate_keystone_id

# Keystone and Hub clocks may differ slightly; never widen the stored bound more.
_CLOCK_SKEW = timedelta(seconds=5)
_DENIED = (ks_exceptions.Unauthorized, ks_exceptions.Forbidden, ks_exceptions.NotFound)


class DelegationError(Exception):
    """A delegation could not be created, verified or used. Never carries secrets."""

    def __init__(self, code: str, detail: str, status_code: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ExportDelegation:
    """Verified reference to one requester-created Trust; not a credential."""

    trust_id: str
    project_id: str
    trustor_user_id: str
    trustee_user_id: str
    role_names: tuple[str, ...]
    expires_at: datetime


def _scope_mismatch(detail: str) -> DelegationError:
    return DelegationError("delegation_scope_mismatch", detail, 403)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_expiry(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def get_image(conn: openstack.connection.Connection, image_id: str):
    return conn.image.get_image(image_id)


def list_image_members(conn: openstack.connection.Connection, image_id: str) -> list[Any]:
    return list(conn.image.members(image_id))


def download_image(conn: openstack.connection.Connection, image_id: str):
    return conn.image.download_image(image_id, stream=True)


def _identity_client(session: ks_session.Session):
    from keystoneclient.v3 import client as ks_client

    return ks_client.Client(session=session, endpoint_override=get_settings().os_auth_url)


def service_trustee_user_id() -> str:
    """Resolve the trustee by authenticating the service identity to its own project only."""
    settings = get_settings()
    auth = v3.Password(
        auth_url=settings.os_auth_url,
        username=settings.os_username,
        password=settings.os_password.get_secret_value(),
        user_domain_name=settings.os_user_domain_name,
        project_name=settings.os_project_name,
        project_domain_name=settings.os_project_domain_name,
        include_catalog=False,
    )
    session = ks_session.Session(auth=auth, timeout=15, verify=settings.ssl_verify)
    try:
        access = auth.get_access(session)
    except Exception:
        raise DelegationError("delegation_unavailable", "Export service identity is unavailable", 503) from None
    try:
        if (
            not access.project_scoped
            or access.trust_scoped
            or access.project_name != settings.os_project_name
            or access.project_domain_name != settings.os_project_domain_name
        ):
            raise ValueError("service identity is not scoped to its own service project")
        return validate_keystone_id(access.user_id)
    except (TypeError, ValueError, KeyError):
        raise DelegationError("delegation_unavailable", "Export service identity is invalid", 503) from None


def _caller_session(auth_ref) -> ks_session.Session:
    settings = get_settings()
    return ks_session.Session(
        auth=AccessInfoPlugin(auth_ref=auth_ref, auth_url=settings.os_auth_url),
        timeout=30,
        verify=settings.ssl_verify,
    )


def _verified_created_trust(
    trust: dict[str, Any],
    *,
    project_id: str,
    trustor_user_id: str,
    trustee_user_id: str,
    role_names: tuple[str, ...],
    latest_expiry: datetime,
) -> ExportDelegation:
    try:
        trust_id = validate_keystone_id(trust.get("id"))
    except (TypeError, ValueError):
        raise _scope_mismatch("Keystone returned an invalid delegation reference") from None
    granted = [role.get("name") for role in trust.get("roles") or [] if isinstance(role, dict)]
    expires_at = _parse_expiry(trust.get("expires_at"))
    if (
        trust.get("trustor_user_id") != trustor_user_id
        or trust.get("trustee_user_id") != trustee_user_id
        or trust.get("project_id") != project_id
        or trust.get("impersonation") is not True
        or not granted
        or not set(granted) <= set(role_names)
        or {str(name).casefold() for name in granted} & FORBIDDEN_DELEGATED_ROLES
        or expires_at is None
        or expires_at > latest_expiry + _CLOCK_SKEW
        or expires_at <= datetime.now(UTC)
    ):
        raise _scope_mismatch("Keystone delegation does not match the requested bounded scope")
    return ExportDelegation(
        trust_id=trust_id,
        project_id=project_id,
        trustor_user_id=trustor_user_id,
        trustee_user_id=trustee_user_id,
        role_names=tuple(sorted(granted)),
        expires_at=expires_at,
    )


def create_export_trust(
    auth_ref,
    *,
    project_id: str,
    trustor_user_id: str,
    role_names: tuple[str, ...],
    ttl_seconds: int,
) -> ExportDelegation:
    """Create and verify the requester's Trust with the caller's already validated session."""
    if not role_names or {name.casefold() for name in role_names} & FORBIDDEN_DELEGATED_ROLES:
        raise DelegationError("delegation_role_invalid", "Delegated export roles are not least privilege", 503)
    trustee_user_id = service_trustee_user_id()
    if trustee_user_id == trustor_user_id:
        raise _scope_mismatch("The export service identity cannot delegate to itself")
    client = _identity_client(_caller_session(auth_ref))
    latest_expiry = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    try:
        created = client.trusts.create(
            trustee_user=trustee_user_id,
            trustor_user=trustor_user_id,
            project=project_id,
            role_names=list(role_names),
            impersonation=True,
            expires_at=latest_expiry,
        )
    except _DENIED:
        raise DelegationError("delegation_denied", "Keystone refused the requester's export delegation", 403) from None
    except Exception:
        raise DelegationError("delegation_unavailable", "Keystone export delegation is unavailable", 503) from None
    body = created.to_dict()
    try:
        return _verified_created_trust(
            body,
            project_id=project_id,
            trustor_user_id=trustor_user_id,
            trustee_user_id=trustee_user_id,
            role_names=role_names,
            latest_expiry=latest_expiry,
        )
    except DelegationError:
        # Never keep a delegation that differs from the admitted scope.
        if isinstance(body.get("id"), str) and body["id"]:
            with suppress(Exception):
                client.trusts.delete(body["id"])
        raise


def delete_trust_as_trustor(auth_ref, trust_id: str) -> bool:
    """Delete an unbound admission Trust with the trustor's original session; True if gone."""
    try:
        _identity_client(_caller_session(auth_ref)).trusts.delete(trust_id)
    except ks_exceptions.NotFound:
        return True
    except Exception:
        return False
    return True


def _trust_session(delegation: ExportDelegation) -> tuple[ks_session.Session, Any]:
    settings = get_settings()
    trustee_user_id = service_trustee_user_id()
    if trustee_user_id != delegation.trustee_user_id:
        raise _scope_mismatch("Stored delegation trustee is not the current export service identity")
    # Trust scope only: no project selectors, so the service password never scopes to the tenant.
    auth = v3.Password(
        auth_url=settings.os_auth_url,
        user_id=trustee_user_id,
        password=settings.os_password.get_secret_value(),
        trust_id=delegation.trust_id,
    )
    session = ks_session.Session(auth=auth, timeout=30, verify=settings.ssl_verify)
    try:
        access = auth.get_access(session)
    except _DENIED:
        raise DelegationError("delegation_revoked", "Requester export delegation is revoked or expired", 403) from None
    except Exception:
        raise DelegationError("delegation_unavailable", "Keystone export delegation is unavailable", 503) from None
    try:
        token = access._data["token"]
        trust = token.get("OS-TRUST:trust") or {}
        if (
            not access.trust_scoped
            or access.trust_id != delegation.trust_id
            or access.trustee_user_id != delegation.trustee_user_id
            or access.trustor_user_id != delegation.trustor_user_id
            or trust.get("impersonation") is not True
            or access.user_id != delegation.trustor_user_id
            or access.project_id != delegation.project_id
            or access.system_scoped
            or access.domain_scoped
        ):
            raise ValueError("trust token scope mismatch")
        expires = access.expires
        if expires is None or expires <= datetime.now(UTC) or expires > delegation.expires_at + _CLOCK_SKEW:
            raise ValueError("trust token expiry exceeds delegation")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _scope_mismatch("Keystone delegation token does not match the stored export scope") from None
    return session, access


def open_trust_connection(
    delegation: ExportDelegation, *, allowed_role_names: frozenset[str]
) -> openstack.connection.Connection:
    """Return a Glance-capable connection limited to the verified trust token."""
    session, access = _trust_session(delegation)
    roles = set(access.role_names or [])
    if (
        not roles
        or not set(delegation.role_names) <= roles
        or not roles <= allowed_role_names
        or {name.casefold() for name in roles} & FORBIDDEN_DELEGATED_ROLES
    ):
        raise _scope_mismatch("Keystone delegation token carries roles outside the stored export scope")
    settings = get_settings()
    try:
        return openstack.connection.Connection(
            session=session,
            region_name=settings.os_region_name,
            interface=settings.os_interface,
            api_timeout=30,
        )
    except Exception:
        raise DelegationError("delegation_unavailable", "Delegated OpenStack access is unavailable", 503) from None


def delete_export_trust(delegation: ExportDelegation) -> bool:
    """Delete a retired Trust through its own impersonating token; False when not yet possible.

    Keystone lets the trustor delete a trust; an impersonating trust token acts as
    that trustor. A trust that cannot issue a token (trustor roles removed, user
    disabled, already deleted) cannot be deleted here and stays bounded by expiry.
    """
    try:
        session, _ = _trust_session(delegation)
    except DelegationError as exc:
        if exc.code == "delegation_unavailable":
            raise
        return False
    try:
        _identity_client(session).trusts.delete(delegation.trust_id)
    except ks_exceptions.NotFound:
        return True
    except _DENIED:
        return False
    except Exception:
        raise DelegationError("delegation_unavailable", "Keystone export delegation is unavailable", 503) from None
    return True
