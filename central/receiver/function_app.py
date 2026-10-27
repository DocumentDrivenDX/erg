"""Python-v2 Azure Function broker. Disabled until infrastructure qualification."""

import logging
import os
from functools import lru_cache

import azure.functions as func
from erg_central.auth import EntraVerifier
from erg_central.blob import BlobStore
from erg_central.receiver import Conflict, Forbidden, Receiver, deployment_binding
from erg_central.status import delivery_status

from erg import delivery_protocol as wire

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
# Anonymous means the app verifies Entra JWT itself; no unauthenticated ingest.


@lru_cache(maxsize=4)
def configured(account, spool, control, prefix, tenant, audience, role):
    return (BlobStore(account, spool, control, prefix), EntraVerifier(tenant, audience, role))


def components(request):
    if os.environ.get("ERG_INGEST_ENABLED") != "true":
        raise Forbidden("ingestion disabled")
    store, verifier = configured(
        os.environ["ERG_STORAGE_ACCOUNT_URL"],
        os.environ["ERG_SPOOL_CONTAINER"],
        os.environ["ERG_CONTROL_CONTAINER"],
        os.environ.get("ERG_SPOOL_PREFIX", "v1"),
        os.environ["ERG_TENANT_ID"],
        os.environ["ERG_INGEST_AUDIENCE"],
        os.environ.get("ERG_REQUIRED_ROLE", "Erg.Collector"),
    )
    # Refresh the trusted registry for revocation on every submission. Cache JWKS/SDK clients.
    registry = wire.decode(store.read_control(os.environ["ERG_ENROLLMENT_REGISTRY_BLOB"]) or b"{}")
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise Forbidden("credential required")
    identity = verifier.verify(authorization[7:], registry)
    identity = deployment_binding(
        identity,
        os.environ.get("ERG_ENROLLMENT_POLICY", "synthetic-only"),
        {key: os.environ.get("ERG_FORWARD_" + key.upper()) for key in wire.ENROLLMENT},
    )
    return Receiver(store), identity


def respond(value, status):
    return func.HttpResponse(wire.encode(value), status_code=status, mimetype="application/json")


@app.route(route="v1/batches", trigger_arg_name="request", methods=["POST"])
def accept(request):
    try:
        if (
            request.headers.get("Content-Type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            return respond(dict(error="application/json required"), 400)
        if request.headers.get("Content-Encoding", "identity") != "identity":
            return respond(dict(error="request compression unsupported"), 400)
        if int(request.headers.get("Content-Length", "0")) > wire.MAX_BATCH:
            return respond(dict(error="batch too large"), 413)
        receiver, identity = components(request)
        raw = request.get_body()
        if len(raw) > wire.MAX_BATCH:
            return respond(dict(error="batch too large"), 413)
        receipt = receiver.accept(raw, request.headers.get("X-Erg-Batch-SHA256", ""), identity)
        receiver.metrics.update(
            registry_reads=1,
            operation_basis="logical application operations; SDK retries/HEADs require Azure meters",
        )
        logging.getLogger("erg.ingest").warning(
            "delivery_metrics %s", wire.encode(receiver.metrics).decode()
        )
        return respond(receipt, 202)
    except Forbidden:
        return respond(dict(error="collector enrollment required"), 403)
    except Conflict:
        return respond(dict(error="immutable delivery conflict"), 409)
    except wire.ProtocolError:
        return respond(dict(error="invalid delivery batch"), 400)
    except Exception:
        # Do not let platform logs serialize request bodies, tokens or SDK errors.
        return respond(dict(error="broker unavailable"), 503)


@app.route(route="v1/delivery/status", trigger_arg_name="request", methods=["GET"])
def status(request):
    try:
        receiver, identity = components(request)
        result = delivery_status(
            receiver.store,
            identity,
            limit=int(request.params.get("limit", "100")),
            cursor=request.params.get("cursor"),
            batch_id=request.params.get("batch_id"),
        )
        return respond(result, 200)
    except Forbidden:
        return respond(dict(error="collector enrollment required"), 403)
    except (wire.ProtocolError, ValueError):
        return respond(dict(error="invalid status request"), 400)
    except Exception:
        return respond(dict(error="status unavailable"), 503)
