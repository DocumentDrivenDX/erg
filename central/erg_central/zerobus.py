"""Documented Zerobus JSON REST with explicitly scoped server-only OAuth."""

import base64
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from erg import delivery_protocol as wire


def error_reason(value):
    """Classify service diagnostics without logging arbitrary response content."""
    message = value.get("message", "")
    if not isinstance(message, str):
        return "unknown"
    message = message.lower()
    for reason, markers in (
        (
            "oauth_scope",
            ("insufficient_scope", "insufficient scope", "required scope", "invalid scope"),
        ),
        (
            "private_storage_access",
            ("storage firewall", "storage account", "private endpoint", "network access"),
        ),
        ("workspace_entitlement", ("workspace access", "workspace-access", "entitlement")),
        (
            "table_privileges",
            (
                "use catalog",
                "use_catalog",
                "use schema",
                "use_schema",
                "modify privilege",
                "select privilege",
                "permission on table",
            ),
        ),
        ("token_audience", ("audience",)),
        ("feature_unavailable", ("not enabled", "not supported", "feature disabled")),
    ):
        if any(marker in message for marker in markers):
            return reason
    return "unknown"


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def table_name(catalog, schema, table):
    if not all(NAME.fullmatch(v) for v in (catalog, schema, table)):
        raise ValueError("invalid owned table identifier")
    return ".".join((catalog, schema, table))


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HTTPS origin required")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Zerobus:
    def __init__(
        self,
        workspace,
        workspace_id,
        endpoint,
        table,
        client_id,
        client_secret,
        oauth_scope="unity-catalog",
    ):
        self.workspace = origin(workspace)
        self.endpoint = origin(endpoint)
        if (
            not workspace_id.isdigit()
            or len(table.split(".")) != 3
            or not all(NAME.fullmatch(v) for v in table.split("."))
        ):
            raise ValueError("invalid Zerobus destination")
        if not client_id or not client_secret:
            raise ValueError("qualified processor credential required")
        self.workspace_id = workspace_id
        self.table = table
        if oauth_scope != "unity-catalog":
            raise ValueError("OAuth scope is not qualified for synthetic pilot")
        self.oauth_scope = oauth_scope
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.expires = 0
        self.metrics = dict(
            oauth_requests=0, insert_requests=0, insert_records=0, insert_body_bytes=0
        )
        self.opener = urllib.request.build_opener(NoRedirect)

    def _request(self, request):
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError()
                return wire.decode(raw) if raw else {}
        except urllib.error.HTTPError as error:
            safe = "unclassified"
            reason = "unknown"
            try:
                value = wire.decode(error.read(65536))
                reason = error_reason(value)
                code = value.get("error") or value.get("error_code")
                if code in (
                    "invalid_scope",
                    "invalid_resource",
                    "invalid_client",
                    "invalid_request",
                    "unauthorized_client",
                    "PERMISSION_DENIED",
                    "RESOURCE_DOES_NOT_EXIST",
                    "INVALID_PARAMETER_VALUE",
                ):
                    safe = code
            except Exception:
                pass
            kind = "token" if request.full_url.endswith("/oidc/v1/token") else "insert"
            raise RuntimeError(
                "Zerobus %s HTTP %d (%s; reason=%s; requested_audience_match=%s); credential scope unchanged"
                % (
                    kind,
                    error.code,
                    safe,
                    reason,
                    self.metrics.get("token_audience_matches_requested_resource"),
                )
            ) from None
        except Exception:
            raise RuntimeError(
                "Zerobus request failed; no materialization acknowledgement"
            ) from None

    def _token(self):
        if self.token and self.expires > time.time() + 60:
            return self.token
        catalog, schema, _ = self.table.split(".")
        details = [
            dict(
                type="unity_catalog_privileges",
                privileges=["USE CATALOG"],
                object_type="CATALOG",
                object_full_path=catalog,
            ),
            dict(
                type="unity_catalog_privileges",
                privileges=["USE SCHEMA"],
                object_type="SCHEMA",
                object_full_path=catalog + "." + schema,
            ),
            dict(
                type="unity_catalog_privileges",
                privileges=["SELECT", "MODIFY"],
                object_type="TABLE",
                object_full_path=self.table,
                operations=["zerobuswrite"],
            ),
        ]
        data = urllib.parse.urlencode(
            dict(
                grant_type="client_credentials",
                scope=self.oauth_scope,
                resource="api://databricks/workspaces/"
                + self.workspace_id
                + "/zerobusDirectWriteApi",
                authorization_details=wire.encode(details).decode("utf-8"),
            )
        ).encode()
        basic = base64.b64encode((self.client_id + ":" + self.client_secret).encode()).decode()
        self.metrics["oauth_requests"] += 1
        value = self._request(
            urllib.request.Request(
                self.workspace + "/oidc/v1/token",
                data=data,
                headers={
                    "Authorization": "Basic " + basic,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
        )
        token = value.get("access_token")
        lifetime = value.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(lifetime, (int, float))
            or lifetime <= 0
        ):
            raise RuntimeError("invalid scoped OAuth response")
        # Diagnostic booleans only: never emit tokens, arbitrary claims or error text.
        self.metrics["token_audience_matches_requested_resource"] = None
        try:
            import json

            parts = token.split(".")
            if len(parts) == 3:
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                aud = claims.get("aud")
                expected = (
                    "api://databricks/workspaces/" + self.workspace_id + "/zerobusDirectWriteApi"
                )
                self.metrics["token_audience_matches_requested_resource"] = (
                    expected == aud or isinstance(aud, list) and expected in aud
                )
        except Exception:
            pass
        self.token = token
        self.expires = time.time() + min(lifetime, 3600)
        return token

    def insert(self, records):
        raw = wire.encode(records)
        if len(raw) > wire.MAX_BATCH:
            raise ValueError("bounded Zerobus request exceeded")
        request = urllib.request.Request(
            self.endpoint + "/zerobus/v1/tables/" + self.table + "/insert",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._token(),
            },
            method="POST",
        )
        self.metrics["insert_requests"] += 1
        self.metrics["insert_records"] += len(records)
        self.metrics["insert_body_bytes"] += len(raw)
        self._request(request)
        # No cursor update: REST success alone never proves Delta visibility.
