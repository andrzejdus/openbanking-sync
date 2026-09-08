"""Thin Enable Banking REST client.

Auth is a short-lived RS256 JWT signed with the application's private key;
`kid` is the application id. See https://enablebanking.com/docs/api/reference/
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import jwt as pyjwt
import requests

from .config import Config

JWT_TTL_SECONDS = 3600


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{path} -> HTTP {status}: {body}")
        self.status = status
        self.body = body
        self.path = path


class EnableBanking:
    def __init__(self, config: Config):
        self._config = config
        self._session = requests.Session()
        self._jwt: str | None = None
        self._jwt_expires_at = 0.0

    # -- auth ---------------------------------------------------------------

    def _token(self) -> str:
        now = time.time()
        if self._jwt is None or now >= self._jwt_expires_at - 60:
            iat = int(now)
            self._jwt = pyjwt.encode(
                {
                    "iss": "enablebanking.com",
                    "aud": "api.enablebanking.com",
                    "iat": iat,
                    "exp": iat + JWT_TTL_SECONDS,
                },
                self._config.private_key,
                algorithm="RS256",
                headers={"kid": self._config.application_id},
            )
            self._jwt_expires_at = iat + JWT_TTL_SECONDS
        return self._jwt

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{self._config.api_origin}{path}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        headers.update(kwargs.pop("headers", {}))
        response = self._session.request(method, url, headers=headers, timeout=60, **kwargs)
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text, path)
        return response.json()

    # -- endpoints ----------------------------------------------------------

    def application(self) -> dict:
        return self._request("GET", "/application")

    def aspsps(self, country: str | None = None) -> list[dict]:
        params = {"country": country} if country else None
        return self._request("GET", "/aspsps", params=params).get("aspsps", [])

    def start_authorization(
        self,
        aspsp_name: str,
        country: str,
        redirect_url: str,
        valid_until: datetime,
        psu_type: str = "personal",
        state: str | None = None,
    ) -> tuple[str, str]:
        """Return (authorization_url, state)."""
        state = state or str(uuid.uuid4())
        body = {
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        return self._request("POST", "/auth", json=body)["url"], state

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> dict:
        return self._request("DELETE", f"/sessions/{session_id}")

    def account_details(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/details")

    def balances(self, account_uid: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(
        self,
        account_uid: str,
        date_from: str | None = None,
        date_to: str | None = None,
        strategy: str | None = None,
    ) -> Iterator[dict]:
        """Yield transactions, following continuation keys until the bank runs out."""
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if strategy:
            params["strategy"] = strategy
        while True:
            page = self._request(
                "GET", f"/accounts/{account_uid}/transactions", params=params
            )
            yield from page.get("transactions", [])
            continuation_key = page.get("continuation_key")
            if not continuation_key:
                return
            params["continuation_key"] = continuation_key


def max_consent_validity(aspsp: dict, requested_days: int) -> datetime:
    """Clamp the requested consent window to what this bank actually allows.

    /aspsps reports `maximum_consent_validity` in seconds; banks vary between
    90 and 180 days and reject anything longer outright.
    """
    requested = timedelta(days=requested_days)
    allowed_seconds = aspsp.get("maximum_consent_validity")
    if allowed_seconds:
        # Shave an hour so clock skew between us and the bank cannot overshoot.
        allowed = timedelta(seconds=int(allowed_seconds)) - timedelta(hours=1)
        requested = min(requested, allowed)
    return datetime.now(timezone.utc) + requested
