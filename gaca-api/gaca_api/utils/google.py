"""Verify a Google ID token for this server. Shared by sign-in and the setup claim."""
from __future__ import annotations

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from ..config import settings


class GoogleAuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def verify_google_identity(raw_id_token: str) -> dict:
    """`{"sub", "email", "name"}` from a Google ID token, or GoogleAuthError.

    The token arrives from the client, so it is verified here and never trusted as-is:
    `verify_oauth2_token` checks Google's signature, that the audience is *our* client id,
    and that it hasn't expired. An unverified Google email is refused: otherwise someone
    could sign up to Google with an address they don't own and use it to claim the matching
    account here.
    """
    if not settings.google_client_id:
        raise GoogleAuthError(500, "Google login not configured")
    try:
        idinfo = google_id_token.verify_oauth2_token(
            raw_id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError:
        # Bad signature, wrong audience, or expired — indistinguishable to the caller on purpose.
        raise GoogleAuthError(401, "Invalid Google token")
    sub, email = idinfo.get("sub"), idinfo.get("email")
    if not sub or not email or not idinfo.get("email_verified", False):
        raise GoogleAuthError(401, "Google token missing required claims")
    return {"sub": sub, "email": email, "name": idinfo.get("name") or email.split("@")[0]}
