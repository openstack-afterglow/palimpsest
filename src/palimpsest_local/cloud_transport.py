"""Transport policy for original-caller cloud mutations, not an auth shortcut."""

from keystoneauth1.session import Session

from .errors import PalimpsestError


class OneShotSession(Session):
    """Never silently resend a provider mutation after an uncertain result."""

    def request(self, url, method, **kwargs):
        mutation = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
        if mutation:
            # Service-specific defaults (Glance defaults to five status retries)
            # and auth/redirect handling must not bypass durable effect intents.
            kwargs.update(
                connect_retries=0,
                status_code_retries=0,
                allow_reauth=False,
                redirect=False,
            )
        response = super().request(url, method, **kwargs)
        if mutation and 300 <= response.status_code < 400:
            response.close()
            raise PalimpsestError("cloud mutation redirect refused; preserve the effect intent")
        return response
