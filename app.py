"""ServiceNow stand-in for the Camunda ServiceNow blueprint.

Implements the exact surface the blueprint's connectors call:

  POST   /api/now/table/{table}          Table API create   -> {"result": {"sys_id": ...}}
  GET    /api/now/table/{table}          Table API read     -> {"result": [...]}
  PATCH  /api/now/table/{table}/{sys_id} Table API update
  DELETE /api/now/table/{table}/{sys_id} Table API delete
  POST   /api/camun/{flow}               Flow Trigger stand-in -> {"result": {"executionId": ...}}
  GET    /simulate-error/{code}          deterministic HTTP error for the error-boundary demo

...and plays the part of a ServiceNow agent by calling BACK into Camunda:

  POST   /sim/complete/{sys_id}          publish the right BPMN message for that record
  GET    /sim/records                    inspect everything created so far

Every inbound call is Basic-authenticated against SN_USER / SN_PWD so the
{{secrets.snUser}} / {{secrets.snPwd}} path in the model is genuinely exercised.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s")
log = logging.getLogger("sn-stub")

SN_USER = os.getenv("SN_USER", "snuser")
SN_PWD = os.getenv("SN_PWD", "snpwd")
CAMUNDA = os.getenv("CAMUNDA_REST_BASE", "http://orchestration:8080/v2").rstrip("/")
# OIDC client-credentials against Keycloak. Reuses the `connectors` client, which
# already carries the orchestration-api audience.
OIDC_TOKEN_URL = os.getenv(
    "OIDC_TOKEN_URL",
    "http://keycloak:18080/auth/realms/camunda-platform/protocol/openid-connect/token")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "connectors")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "orchestration-api")
AUTO_COMPLETE = float(os.getenv("AUTO_COMPLETE_SECONDS", "6"))
MSG_TTL_MS = int(os.getenv("MESSAGE_TTL_MS", "300000"))  # 5 min buffer

# table -> BPMN message name the blueprint waits on after creating a record there
TABLE_MESSAGE = {
    "change_request": "changeRequestDone",
    "sc_task": "catalogTaskDone",
}

_token: dict[str, Any] = {"value": None, "expires": 0.0}


async def bearer() -> str | None:
    """Fetch and cache an access token; returns None when OIDC is not configured."""
    if not OIDC_CLIENT_SECRET:
        return None
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(OIDC_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            "audience": OIDC_AUDIENCE,
        })
    r.raise_for_status()
    body = r.json()
    _token["value"] = body["access_token"]
    _token["expires"] = time.time() + float(body.get("expires_in", 300))
    log.info("oidc       token acquired, expires in %ss", body.get("expires_in"))
    return _token["value"]


app = FastAPI(title="ServiceNow stub", version="1.1")
basic = HTTPBasic(auto_error=True)
RECORDS: dict[str, dict[str, Any]] = {}


def auth(creds: HTTPBasicCredentials = Depends(basic)) -> str:
    ok_u = secrets.compare_digest(creds.username, SN_USER)
    ok_p = secrets.compare_digest(creds.password, SN_PWD)
    if not (ok_u and ok_p):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ServiceNow credentials")
    return creds.username


def sys_id() -> str:
    return uuid.uuid4().hex  # 32 chars, same shape as a real sys_id


async def publish(name: str, correlation_key: str, variables: dict | None = None) -> None:
    """Publish a BPMN message to Camunda.

    Uses the BUFFERED publication endpoint, not correlation: ServiceNow can and does
    call back faster than the token reaches the catch event. Buffering removes that race.
    """
    url = f"{CAMUNDA}/messages/publication"
    payload = {
        "name": name,
        "correlationKey": correlation_key,
        "timeToLive": MSG_TTL_MS,
        "variables": variables or {},
    }
    headers = {}
    try:
        tok = await bearer()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    except Exception as exc:  # noqa: BLE001
        log.error("oidc       token request failed: %s", exc)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload, headers=headers)
        if r.status_code < 300:
            log.info("-> camunda  publish %-20s key=%s  [%s]", name, correlation_key, r.status_code)
        else:
            log.error("-> camunda  publish %s FAILED [%s] %s", name, r.status_code, r.text[:300])
    except Exception as exc:  # noqa: BLE001
        log.error("-> camunda  publish %s unreachable: %s", name, exc)


async def agent(sid: str, table: str, delay: float) -> None:
    """Stand in for a human closing the record in ServiceNow."""
    await asyncio.sleep(delay)
    msg = TABLE_MESSAGE.get(table)
    if not msg:
        return
    rec = RECORDS.get(sid)
    if rec:
        rec["state"] = "3"  # closed
    log.info("agent      closed %s/%s", table, sid)
    await publish(msg, sid, {"snClosedAt": time.time(), "snTable": table})


# --------------------------------------------------------------------- Table API
@app.post("/api/now/table/{table}")
async def create(table: str, request: Request, _: str = Depends(auth)):
    body = await request.json() if await request.body() else {}
    sid = sys_id()
    rec = {"sys_id": sid, "table": table, "state": "1",
           "number": f"{table[:3].upper()}{len(RECORDS) + 1000}", **body}
    RECORDS[sid] = rec
    log.info("<- camunda  CREATE %-16s %s  %s", table, sid, body.get("short_description", ""))
    if AUTO_COMPLETE > 0 and table in TABLE_MESSAGE:
        asyncio.create_task(agent(sid, table, AUTO_COMPLETE))
    return {"result": rec}


@app.get("/api/now/table/{table}")
async def read(table: str, request: Request, _: str = Depends(auth)):
    q = request.query_params.get("sysparm_query", "")
    limit = int(request.query_params.get("sysparm_limit") or 100)
    rows = [r for r in RECORDS.values() if r["table"] == table]
    if q.startswith("sys_id="):
        wanted = q.split("=", 1)[1]
        rows = [r for r in rows if r["sys_id"] == wanted]
    log.info("<- camunda  READ   %-16s query=%r -> %d row(s)", table, q, len(rows))
    return {"result": rows[:limit]}


@app.patch("/api/now/table/{table}/{sid}")
async def update(table: str, sid: str, request: Request, _: str = Depends(auth)):
    if sid not in RECORDS:
        raise HTTPException(404, "record not found")
    RECORDS[sid].update(await request.json())
    log.info("<- camunda  UPDATE %-16s %s", table, sid)
    return {"result": RECORDS[sid]}


@app.delete("/api/now/table/{table}/{sid}")
async def delete(table: str, sid: str, _: str = Depends(auth)):
    RECORDS.pop(sid, None)
    log.info("<- camunda  DELETE %-16s %s", table, sid)
    return Response(status_code=204)


# ------------------------------------------------------- Flow Trigger stand-in
@app.post("/api/camun/{flow}")
async def start_flow(flow: str, request: Request, _: str = Depends(auth)):
    """Stands in for the Enterprise-Pack Flow Trigger.

    The blueprint posts {"correlationValue": camId} and then waits on message
    'fromSN' keyed by camId. We echo an executionId and fire that message back.
    """
    body = await request.json() if await request.body() else {}
    cam_id = body.get("correlationValue")
    exec_id = sys_id()
    log.info("<- camunda  FLOW   %-16s camId=%s exec=%s", flow, cam_id, exec_id)
    if cam_id:
        asyncio.create_task(_flow_callback(cam_id, exec_id, flow))
    return {"result": {"executionId": exec_id, "flow": flow}}


async def _flow_callback(cam_id: str, exec_id: str, flow: str) -> None:
    await asyncio.sleep(max(AUTO_COMPLETE / 2, 1))
    await publish("fromSN", cam_id, {"snFlowExecutionId": exec_id, "snFlow": flow})


# ------------------------------------------------------------------- utilities
@app.get("/simulate-error/{code}")
async def simulate_error(code: int):
    """Deterministic failure for the error-boundary branch.

    The blueprint's errorExpression only raises a BPMN error on 407, so the
    original localhost:4711 (connection refused) would never reach the handler.
    """
    return Response(status_code=code, content=f'{{"error":"simulated {code}"}}',
                    media_type="application/json")


@app.post("/sim/complete/{sid}")
async def manual_complete(sid: str):
    rec = RECORDS.get(sid)
    if not rec:
        raise HTTPException(404, "record not found")
    await agent(sid, rec["table"], 0)
    return {"completed": sid, "table": rec["table"]}


@app.get("/sim/records")
async def list_records():
    return {"count": len(RECORDS), "records": list(RECORDS.values())}


@app.get("/health")
async def health():
    return {"ok": True, "camunda": CAMUNDA, "auto_complete_seconds": AUTO_COMPLETE,
            "oidc": bool(OIDC_CLIENT_SECRET), "token_url": OIDC_TOKEN_URL}
