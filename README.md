# Camunda–ServiceNow Integration, Stubbed

Runs Camunda's [ServiceNow Integration Quick Start](https://marketplace.camunda.com/en-US/apps/632605/servicenow-integration-quick-start)
blueprint end-to-end on a **self-managed Camunda 8.8** stack — with no ServiceNow
instance, no ServiceNow Store entitlement, and no IntegrationHub Enterprise Pack.

A Python FastAPI service stands in for ServiceNow. Every outbound call the blueprint
makes is a real HTTP request; every callback into Camunda is a real message
publication against the Orchestration Cluster REST API. What is removed is the
procurement, not the integration.

![ServiceNow Integration Pattern](Images/SNIP.png)

---

## Why stub ServiceNow at all?

The blueprint is bi-directional. Camunda calls ServiceNow, and ServiceNow calls
Camunda back. The outbound half needs nothing special. The return half is where the
blueprint stops being self-service.

### What a "spoke" is

ServiceNow's integration layer is **IntegrationHub**, built on a hub-and-spoke model.
The *hub* is the engine inside Flow Designer that executes integration steps. A
*spoke* is a packaged scoped application containing pre-built Actions for one external
system, surfaced as drag-and-drop steps in Flow Designer.

Spokes are named after the system at the far end, not after who wrote them. The
**Camunda Spoke** is therefore ServiceNow code, running inside ServiceNow, authored by
Camunda as a ServiceNow build partner and distributed through the ServiceNow Store.
Its scope namespace is visible in the setup instructions as `x_camun_camunda.Camunda` —
`x_` for custom scoped app, `camun` for the vendor prefix.

It provides four Flow Designer actions pointing at a Camunda cluster: **Start process**,
**Correlate message**, **Send signal**, **Cancel process**.

### Gate 1 — Store entitlement

A ServiceNow **Personal Developer Instance** is free, and satisfies two prerequisites
outright: it is a full instance on the current release (Yokohama or newer), with an
admin account able to reach target tables and Flow Designer.

What a PDI does **not** have is Store entitlement. The setup instructions say to open
Application Manager, search for Camunda Spoke, and install it — but that only works if
the app is already entitled to the instance. Store apps are requested through an HI
account tied to a customer or partner company, and then appear in Application Manager
on that company's instances. A PDI has no company behind it, so the search returns
nothing and there is no self-service path around it.

### Gate 2 — Enterprise Pack

The blueprint's `Start ServiceNow Flow` task uses the **Flow Starter** connector, which
additionally requires **IntegrationHub Enterprise Pack** and **Flow Trigger – REST**.
The Enterprise Pack Installer is not available on a PDI. That is a licensed SKU, not a
plugin you activate.

The **Starter Pack** and **Action Step – REST** *are* PDI-activatable via the
IntegrationHub Installer — and those are what a Flow would need to call back into
Camunda. So the plugin list gates the *inbound* direction; it does not gate the Table
API calls at all.

### Gate 3 — reachability

A PDI lives in ServiceNow's cloud. A Docker Compose stack lives on a laptop. Nothing
in ServiceNow can reach `localhost:8080`. Closing that loop needs a public tunnel
(cloudflared/ngrok) fronting the orchestration cluster, or a MID Server. In practice
this costs more time than the plugins do.

### Gate 4 — the Spoke assumes SaaS

Even with the Spoke installed, its configuration is shaped for Camunda 8 SaaS. The
OAuth profile wants token URL `https://login.cloud.camunda.io/oauth/token`; the
connection record wants Host set to a cluster **Region ID** (e.g. `lhr-1.zeebe.camunda.io`)
and Base path set to a **Cluster ID**. Credentials come from the SaaS Console's API tab.

None of that exists in a self-managed stack. There is no Console API tab, no region ID,
no cluster ID. You would repoint the token URL at Keycloak, register a client with the
IdP, and grant it authorizations in Orchestration Cluster Admin — plus read and possibly
patch the `OAuthCamundaUtil` script include, which handles Camunda-specific token
mechanics and most likely injects the SaaS `audience` parameter.

This is precisely the *"may require minor adjustments to the Camunda Spoke"* caveat on
Camunda's [prerequisites page](https://docs.camunda.io/docs/components/camunda-integrations/servicenow/prerequisites/).
It is unsupported territory.

### What the stub skips

All four gates. The stub replaces the ServiceNow-side implementation, not the
integration pattern:

| Blueprint capability | Certified path | This repo | Fidelity |
|---|---|---|---|
| CRUD on ServiceNow tables | Outbound Connector → Table API | identical, unchanged | **100%** |
| Incident on process error | Incident Handler | identical | **100%** |
| Start Camunda from ServiceNow | Spoke *Start process* | REST → `/v2/process-instances` | ~95% |
| Correlate back into Camunda | Spoke *Correlate message* | REST → message publication | ~95%, **plus a race fix** |
| Signal / Cancel | Spoke actions | REST | 100% functional |
| Start ServiceNow Flow | Flow Starter (Enterprise Pack) | stub endpoint at same path | ~70% — real loss |

The only genuine capability loss is the Enterprise Pack's async Flow Trigger semantics.
Everything else is packaging, vendor support, and a shorter security-review conversation.

**A correlation fix worth carrying forward.** The Spoke's *Correlate message* action maps
to the correlate-message endpoint, which is explicitly **not buffered**. The blueprint
puts `Start ServiceNow Flow` immediately before a catch on `fromSN`, so a fast callback
can arrive before the token does and be dropped silently. This stub uses the **buffered
publication** endpoint with a TTL instead. That is a fix, not a workaround — carry it
into any real ServiceNow flow you build.

---

## Connectors

All three ServiceNow connectors and the generic REST connector compile to the **same
job type**. Verified from Camunda's own element-template JSON:

| Template | ID | Job type |
|---|---|---|
| ServiceNow Outbound Connector | `io.camunda.connectors.ServiceNow.v1` | `io.camunda:http-json:1` |
| ServiceNow Incident Handler | `io.camunda.connectors.ServiceNowIncident.v1` | `io.camunda:http-json:1` |
| ServiceNow Flow Starter | `io.camunda.connectors.ServiceNowFlow.v1` | `io.camunda:http-json:1` |
| REST Outbound Connector | `io.camunda.connectors.HttpJson.v2` | `io.camunda:http-json:1` |

There is no ServiceNow worker at runtime. The certified ServiceNow connectors are the
generic HTTP connector with a ServiceNow-shaped properties panel over the top — table
dropdowns, a `sysparm_query` field, a pre-built URL expression. That is why this model
runs unchanged with the templates removed.

### Template version drift

The published blueprint references template versions that are not the ones currently on
the Marketplace — in **both** directions:

| Task | Template | Blueprint wants | Marketplace ships |
|---|---|---|---|
| Create / Search Request Item, Change Request, Catalog Task | `ServiceNow.v1` | **7** | 3 |
| Start ServiceNow Flow | `ServiceNowFlow.v1` | **3** | 1 |
| Create Incident | `ServiceNowIncident.v1` | **2** | 3 |
| Simulate error | `HttpJson.v2` | **11** | 13 |

Because the drift runs both ways, no single download resolves it — Modeler needs an
exact id+version match and flags every task as *Template not found*. The REST connector
is worse: v13 declares `"engines": {"camunda": "^8.9"}`, which excludes an 8.8 cluster
outright.

**What was changed:** the `zeebe:modelerTemplate`, `zeebe:modelerTemplateVersion` and
`zeebe:modelerTemplateIcon` attributes were stripped from all seven service tasks, along
with the two dangling `elementTemplateId` / `elementTemplateVersion` task headers. Every
`taskDefinition`, input mapping, secret reference and result expression is untouched.
The tasks render as plain service tasks and behave identically — the model deployed and
ran green for hours with the templates unresolved, which is the clearest possible
demonstration that they are editor metadata.

---

## Defects fixed in the published blueprint

These are not stub plumbing. They would follow you onto a real ServiceNow instance.

**1 · Variable collision.** `crResultSysId` was written by three separate tasks — Flow
Starter (`executionId`), Create Change Request (`sys_id`), Create Catalog Task (`sys_id`).
It only worked because each catch event immediately followed its producer. Any
reordering, parallelisation or retry breaks correlation silently. Split into
`flowExecutionId`, `crSysId`, `catalogTaskSysId`.

**2 · Duplicate message name.** Both intermediate catch events subscribed to
`ServiceNowUserTask` with the same correlation-key expression. Split into
`changeRequestDone` (keyed on `crSysId`) and `catalogTaskDone` (keyed on `catalogTaskSysId`).

**3 · Retry storm.** `retryBackoff` was `PT0S` on all seven connector tasks with
`retries="3"` — three immediate retries against ServiceNow on any transient 5xx. Now `PT5S`.

**4 · Dead error branch.** `Simulate error` targeted `http://localhost:4711`, which from
inside the connectors container is a connection refusal, not an HTTP response — while the
task's `errorExpression` only raises a BPMN error on **407**. The incident branch could
never fire. Now targets a stub endpoint returning a real 407.

> The 407 is itself a clue: *Proxy Authentication Required* means the sample was authored
> behind a corporate proxy. On a corporate network you may see genuine 407s from the
> connector runtime in Phase 2, indistinguishable from the simulated one.

**5 · Housekeeping.** Orphan message `Message_2dq0qkn` (correlation key `=""`) removed;
redundant `resultVariable` headers dropped where a `resultExpression` already existed;
execution platform bumped to 8.8.

---

## What the stub implements

`app.py` plays the ServiceNow pool. Note that `Process_snstub_v4_sn` — the green lane in
the diagram — is `isExecutable="false"`. Its tasks, events and data stores have zero
runtime behaviour; they are documentation. On a real instance those boxes are Flow
Designer work you still have to build. Here, the stub is that implementation.

```
POST   /api/now/table/{table}            Table API create   -> {"result": {"sys_id": ...}}
GET    /api/now/table/{table}            Table API read     -> {"result": [...]}
PATCH  /api/now/table/{table}/{sys_id}   Table API update
DELETE /api/now/table/{table}/{sys_id}   Table API delete
POST   /api/camun/{flow}                 Flow Trigger stand-in -> {"result": {"executionId": ...}}
GET    /simulate-error/{code}            deterministic HTTP error for the error branch

POST   /sim/complete/{sys_id}            play the agent: close the record, publish the message
GET    /sim/records                      inspect everything created so far
GET    /health                           readiness + OIDC status
```

Inbound calls are Basic-authenticated against `SN_USER` / `SN_PWD`, so the
`{{secrets.snUser}}` / `{{secrets.snPwd}}` path in the model is genuinely exercised.
Outbound callbacks authenticate to Camunda with OIDC client credentials.

---

## Run

```bash
pip install fastapi uvicorn httpx
```

```bash
export SN_USER=snuser
export SN_PWD=snpwd
export CAMUNDA_REST_BASE=http://localhost:8088/v2
export OIDC_TOKEN_URL=http://localhost:18080/auth/realms/camunda-platform/protocol/openid-connect/token
export OIDC_CLIENT_ID=connectors
export OIDC_CLIENT_SECRET=<from your stack .env>
export OIDC_AUDIENCE=orchestration-api
export AUTO_COMPLETE_SECONDS=0

python3 -m uvicorn app:app --host 0.0.0.0 --port 8001
```

`curl -s localhost:8001/health | jq` must show `"oidc": true`.

Secrets resolve on the **connector runtime**, not the broker. If your runtime sets a
custom prefix in `application.yaml`, match it:

```yaml
camunda:
  connector:
    secretprovider:
      environment:
        prefix: "CONNECTORS_SECRET_"
```

```
CONNECTORS_SECRET_snUser=snuser
CONNECTORS_SECRET_snPwd=snpwd
```

Deploy `servicenow-integration-blueprint-stub.bpmn` and start `Process_snstub_v4`.

### Two run modes

**`AUTO_COMPLETE_SECONDS=6`** — the stub plays an agent closing each record after a
delay. The instance runs start to finish in about 20 seconds, unattended.

**`AUTO_COMPLETE_SECONDS=0`** — records are created and left open. The instance parks on
each message catch event and stays Active indefinitely, like a real catalog task awaiting
a human. Release it yourself:

```bash
curl -s localhost:8001/sim/records | jq '[.records[] | select(.state=="1") | {sys_id, table}]'
curl -s -X POST localhost:8001/sim/complete/<sys_id>
```

Twice per instance — once for `change_request`, once for `sc_task`.

![Instance parked on a ServiceNow-side wait](Images/CRP%202.png)

---

## Observing the integration

Four surfaces, each answering a different question:

- **uvicorn log** — what Camunda asked ServiceNow to do. `<- camunda` lines show table,
  sys_id and payload; `-> camunda` lines show the callback and its HTTP status.
- **`/sim/records`** — the ServiceNow-side data. `state: "1"` open, `"3"` closed.
- **Operate variables** — the correlation contract. `snFlow` and `snClosedAt` did not
  originate in Camunda; they arrived on a message. `snQueryResult` records
  `"server":"uvicorn"` in the response headers — the engine's own evidence that an
  external HTTP service answered.
- **`docker logs connectors`** — the wire, when a task incidents.

Every green ① badge on a catch event corresponds to exactly one record at `state: "1"`.

### Stepping through by hand

Service tasks cannot hold a token — the connector runtime claims the job in milliseconds.
To walk the process manually, stop the worker:

```bash
docker compose -p <project> -f docker-compose-full.yaml stop connectors
```

Now every service task parks. Use Operate's **Modify** tool to move the token and inject
the variable each task would have produced:

| Task | Variable |
|---|---|
| Create Request Item | `requestedItemResultSysId` |
| Search Request Item | `state` |
| Start ServiceNow Flow | `flowExecutionId` |
| Create a Change Request | `crSysId` |
| Create a Catalog Task | `catalogTaskSysId` |

`Generate Custom ID` is a script task — the engine evaluates it and sets `camId` itself,
connectors stopped or not.

Set the variable in the **same batch** as the move onto a catch event: correlation keys
are read when the token arrives.

![Hand-stepping a token with injected variables](Images/CRP3.png)

Moved elements record `state: TERMINATED`, not `COMPLETED` — a Move is *cancel at source,
activate at target*, so the task never did its work. That is also why an interrupting
boundary event terminates its activity rather than completing it.

---

## Phase 2 — cutting over to a real instance

The transform introduced one indirection to make the switch a single field:

```
snInstance = "stub"
snBaseUrl  = if snInstance = "stub"        then "http://host.docker.internal:8001"
        else if snInstance = "stub-docker" then "http://sn-stub:8000"
        else "https://" + snInstance + ".service-now.com"
```

Set `snInstance` to a real instance name and every Table API task retargets. Then:

1. Point the connector secrets at the real ServiceNow account.
2. Expose the cluster to ServiceNow (cloudflared/ngrok, or a MID Server).
3. Rebuild the callbacks as Flow Designer flows with Action Step – REST, replicating what
   `app.py` does: publish `fromSN`, `changeRequestDone`, `catalogTaskDone`.
4. Revert `Simulate error` if you no longer want a forced failure.

`Start ServiceNow Flow` stays synthetic without the Enterprise Pack. On a PDI, back it
with a Scripted REST API at the same path — the connector never learns the difference.

---

## Verified end-to-end

![End-to-end run on self-managed Camunda 8.8 with no ServiceNow instance](Images/SNCMNE2ESTUBTSTD.png)

All five regions of the blueprint, three message correlations on three distinct keys, the
error boundary firing on a real 407 — on a self-managed cluster with OIDC enforced, zero
ServiceNow entitlement, and zero Enterprise Pack.
