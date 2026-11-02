# Deploying to Azure Container Apps

The container is stateless. State is in Postgres (Supabase): the `booking_agent` schema (agent) and the
`mock_provider` schema (the mock provider's world). Both are created on first start.

## Once

1. Supabase: copy the session pooler connection string (port 5432; the direct host is IPv6-only and
   Container Apps cannot reach it). Percent-encode special characters in the password. Deploy in the Azure
   region nearest the database.
2. `cp .env.example .env` and set `BA_DATABASE_URL`, `BA_ACCESS_CODE` and `OPENAI_API_KEY`.
3. `az login`.

## Deploy and redeploy

```bash
scripts/deploy_azure.sh
```

This creates `rg-bookingagent` in France Central, builds the Dockerfile in Azure Container Registry and
deploys one replica with HTTPS ingress. The database URL, access code and LLM key are Container Apps secrets.
It prints the URL and checks `/healthz`. Override names with `RG`, `LOC`, `APP`, `ENVNAME`.

## Operating

- Logs: `az containerapp logs show -n bookingagent -g rg-bookingagent --follow`
- Stop compute billing: `az containerapp update -n bookingagent -g rg-bookingagent --min-replicas 0`
- Remove everything: `az group delete -n rg-bookingagent` (the Supabase data stays).

## One replica

Keep one replica. The SSE live-update bus and the rate limiter are in process, so a second replica would
miss updates and count requests separately. Locks use Postgres advisory locks and do not need this limit.

## Observability on Azure

- Console output goes to Log Analytics workspace `workspace-rgbookingagentYbaN`: uvicorn request lines, and
  `enqueue_failed`, `job_dead`, `job ... failed on attempt N`, `turn failed` and provider-cancellation watcher lines.
- Per-turn and per-job traces (model calls with tokens and cost, tool calls, `quote_status`, `job_status`,
  `booking_created`, latency) are JSONL files in the container at `/tmp/traces` and in memory. They show on
  `/admin/`. They are not in Log Analytics and are lost when the container restarts.
- Live: Container Apps > bookingagent > Monitoring > Log stream, or
  `az containerapp logs show -n bookingagent -g rg-bookingagent --follow`.
- Search: Container app > Monitoring > Logs.

```kusto
ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'bookingagent' | project TimeGenerated, Log_s | order by TimeGenerated desc
```

```kusto
ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'bookingagent' and Log_s has_any ('ERROR', 'enqueue_failed', 'job_dead', 'failed on attempt')
```

```kusto
ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'bookingagent' and Log_s has 'POST /v1/chat'
```

- Alerts: Monitor > Alerts > Alert rules, `bookingagent-enqueue_failed` and `bookingagent-job_dead`. See Alerts below.
- Metrics: Container app > Monitoring > Metrics (requests, CPU, memory, replicas).
- Per-booking traces: `/admin/` has turn timelines, a Jobs tab (attempts, last error), quotes, bookings and users.
  A demo control can force a timeout-after-success.
- Known gap: traces are not in Log Analytics and do not survive restarts. Writing each trace event as a JSON log
  line would make them searchable there.

## Alerts

Two Azure Monitor scheduled query rules, created through the REST API: `bookingagent-enqueue_failed` and
`bookingagent-job_dead`.

- Severity 1, evaluated every 5 minutes over the last 5 minutes, fire when count > 0, auto-resolve.
- Query: `ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'bookingagent' and Log_s has '<name>'`, with
  `<name>` set to `enqueue_failed` or `job_dead` (the ERROR lines the app writes).
- No action group yet. Fired alerts show in the portal under Monitor > Alerts. Attach an action group to get email.
- The subscription needed `az provider register -n Microsoft.Insights` first.
