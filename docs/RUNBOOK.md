# Pulso platform runbook

Operational procedures for the Pulso services on Kubernetes. Every command assumes
`-n pulso`; `make status` shows pods, HPAs, PDBs and recent events. Alerts link straight
to the sections below.

- [Incident checklist](#incident-checklist)
- [Deploy](#deploy)
- [Roll back](#roll-back)
- [Scaling playbook](#scaling-playbook)
- [Troubleshooting](#troubleshooting)

## Incident checklist

Work through these in order. The point is to stop the bleeding first and understand later.

1. **Acknowledge and open a channel.** Ack the page, open `#inc-<date>-<summary>` and name
   one incident lead. The lead coordinates and does not debug.
2. **Size the impact.** Open Grafana "Pulso / Services (RED)" and note which services have
   a 5xx ratio or p95 above normal, since when, and whether it is every route or one.
   Merchants failing to create orders (orders-api) is SEV1. Support replies degraded
   (support-agents) is SEV2.
   The dashboard covers orders-api and matching. For support-agents and saga, use pod
   status, restarts and logs.
3. **Look for a recent change.** Most incidents follow one.
   ```bash
   kubectl argo rollouts get rollout orders-api        # if the canary component is enabled
   argocd app history pulso-prod                       # what synced, when
   kubectl rollout history deployment/<svc>
   kubectl get events --sort-by=.lastTimestamp | tail -30
   ```
4. **Mitigate before you diagnose.** If a deploy lines up with the start of the impact,
   [roll back](#roll-back) now. If it is load, follow the [scaling playbook](#scaling-playbook).
   If one pod is bad, delete it and let the ReplicaSet replace it.
5. **Check the dependencies.** Look at the managed Postgres and Redis dashboards (connections,
   CPU, failover events). Look at `kubectl get externalsecret`, since a failed secret
   refresh breaks new pods only. Look at the LLM provider's status page for support-agents.
6. **Communicate every 30 minutes** in the channel, even when there is nothing new: impact,
   current hypothesis, next step.
7. **Confirm recovery** on the RED dashboard: error ratio and p95 back to baseline for
   15 minutes. Then resolve.
8. **Write the postmortem within 48 hours.** Keep it blameless and cover the timeline,
   root cause, what detected it and what should have, plus action items with owners.
   Add any manual step you needed during the incident to this runbook.

## Deploy

**GitOps (normal path).** Argo CD owns both environments (`argocd/apps`):

- **dev** follows `main`. A merged PR syncs automatically, with prune and self-heal.
- **prod** follows the `prod` branch. A promotion is a PR that bumps the image tags in
  `k8s/overlays/prod/kustomization.yaml` and merges to `prod`. Automated syncs run only
  inside the working-hours window in the AppProject. Manual syncs are always allowed.

Sync order inside an Application:

| Wave | What |
|---|---|
| -3 | Namespace and SecretStore |
| -2 | Secrets, ExternalSecrets and dev data stores |
| -1 | `orders-api-migrate` Sync hook |
| 0 | Deployments and everything else |

A failed migration stops the sync before any new pod starts.

**Without Argo CD** (kind, break-glass):

```bash
kustomize build k8s/overlays/prod | kubectl apply -f -   # or: make deploy-dev
NAMESPACE=pulso ./scripts/wait-ready.sh                   # data stores, Jobs, Deployments
```

**Helm alternative:** `make helm-deploy-dev`, or
`helm upgrade --install pulso charts/pulso -n pulso -f charts/pulso/values-prod.yaml`.

Deployments roll with `maxUnavailable: 0, maxSurge: 1`. Capacity never drops, and a pod
that never becomes ready stops the rollout on its own.

**Canary (orders-api, when `k8s/components/orders-api-canary` is enabled).** Traffic moves
10% → 25% → 50% while the AnalysisTemplate checks the canary's success rate every minute,
then waits for a human:

```bash
kubectl argo rollouts get rollout orders-api --watch
kubectl argo rollouts promote orders-api       # after the final pause
```

## Roll back

Pick the fastest option that matches how the change went out.

| Situation | Action |
|---|---|
| Canary still in progress | `kubectl argo rollouts abort orders-api` sends all traffic back to stable at once |
| Canary already promoted | `kubectl argo rollouts undo orders-api` |
| Prod synced by Argo CD | Revert the promotion commit on `prod` and let Argo CD sync. For an emergency: `argocd app rollback pulso-prod <history-id>` (disables auto-sync until re-enabled) |
| Applied with kubectl / kind | `make rollback SVC=orders-api` runs `rollout undo` and waits |
| Helm release | `helm rollback pulso <revision> -n pulso` |

After any rollback that did not go through git, revert the tag in git too. Otherwise
self-heal or the next apply rolls forward again.

Migrations do not roll back on their own. Prisma migrations should be expand/contract: the
previous image must keep working on the new schema. If a migration itself is the problem,
fix forward with a new migration rather than editing the applied one.

## Scaling playbook

Use this when the cause is load and nothing is broken: the RED dashboard shows rate up,
p95 up and errors flat, `PulsoHighLatency` or `PulsoHPAAtMaximum` fires, or a known peak
is coming (marketing push, holiday).

1. **Confirm it is load, not a dependency.** If p95 rose without more traffic, look at
   Postgres and Redis first. More pods would only add connections to a struggling database.
2. **Check headroom.** Run `kubectl get hpa`. If the HPA is below max, it is already
   scaling: give it 2–3 minutes, since scale-up adds up to 2 pods a minute.
3. **HPA at max (orders-api, matching).** Raise the ceiling and floor in git
   (`k8s/overlays/prod/patches/hpa.yaml`, or per service). For an immediate effect, patch
   the live object and backport the change:
   ```bash
   kubectl patch hpa orders-api --type merge -p '{"spec":{"maxReplicas":20,"minReplicas":6}}'
   ```
   `make scale SVC=matching REPLICAS=6` raises only the floor.
4. **Pods Pending after scaling.** The cluster is out of capacity. The node autoscaler
   should add nodes within minutes. If it hits its own max, raise it in the cloud
   account, or lower requests only after checking `kubectl top pod` against requests.
5. **saga.** It has no HPA, so scale it by hand: `kubectl scale deployment/saga --replicas=3`.
   Then update `replicas:` in the prod overlay. Redis Streams consumer groups share the
   work between replicas.
6. **support-agents.** Do not scale out. Conversation checkpoints live in per-pod SQLite,
   so a second replica would split threads. Give it more CPU and memory instead (prod
   patch) until the checkpointer moves to Postgres.
7. **Database limits.** Every orders-api pod holds its own Prisma pool. Before going past
   about 20 pods, check `max_connections` on the managed Postgres, or put PgBouncer or
   RDS Proxy in front.
8. **Planned peaks.** Raise `minReplicas` the day before, in git, and lower it afterwards.
   Scaling up on demand is reactive and takes minutes.

## Troubleshooting

### CrashLoopBackOff

The container starts and exits.

```bash
kubectl describe pod <pod>          # Last State: exit code and reason
kubectl logs <pod> --previous       # output of the crashed attempt
```

- **orders-api exits at boot** with `JWT_SECRET must be at least 32 characters` or
  `DATABASE_URL is required`. The Secret is incomplete. Run `kubectl get externalsecret`
  in prod, or check the dev generator.
- **A write error such as `EROFS`.** The app writes outside `/tmp` (and `/data` for
  support-agents) while the root filesystem is read-only. Mount an `emptyDir` at that path
  rather than turning `readOnlyRootFilesystem` off.
- **Exit code 1 right after a config change.** Generated ConfigMap names are hashed, so
  `kubectl get cm` shows which version each pod uses.

### Probes failing

These show up as `Readiness probe failed` or `Liveness probe failed` in events.

- **`connection refused`.** The app listens on another port or only on `127.0.0.1`.
  orders-api and saga use `$PORT`. matching and support-agents always listen on `:8000`.
- **404.** The probe path is wrong:
  - orders-api serves `/health/live` (startup, liveness) and `/health/ready` (readiness);
  - support-agents serves `/healthz`;
  - matching and saga serve `/health`.
- **Restarted during boot.** A slow start needs more `startupProbe.failureThreshold`
  (currently 24 x 5 s = 2 min). Liveness only starts after the startup probe passes.
- **orders-api not ready while Postgres is down.** This is expected. `/health/ready` checks
  the database, so readiness takes the pods out of the Service. Liveness uses `/health/live`,
  which ignores dependencies, so a database outage never restarts every pod.
- **orders-api not ready during a rollout's terminations.** This is expected too.
  `/health/ready` answers 503 (`"status": "draining"`) during the 5 s drain after SIGTERM.
- **Ready, then not ready under load.** Readiness is doing what it should and shedding the
  pod. Look at latency and dependencies instead of relaxing the probe.

### OOMKilled

`Last State: Terminated, Reason: OOMKilled`, exit code 137: the container went over
`limits.memory`. The `PulsoContainerOOMKilled` alert fires on it.

```bash
kubectl top pod -l app.kubernetes.io/name=orders-api
```

The Node services cap the V8 heap at about 75% of the limit
(`NODE_OPTIONS=--max-old-space-size`), so a heap leak shows up as a JavaScript heap error
in the logs instead of a silent kill. When you raise the limit in the overlay patch, raise
that value with it. For the FastAPI services, memory grows with `WEB_CONCURRENCY` (one
process per worker).

### Migration Job failed

```bash
kubectl get jobs; kubectl logs job/orders-api-migrate
```

- **`P1001` (cannot reach database).** Check the `orders-api-postgres-egress`
  NetworkPolicy and, in prod, the managed-DB CIDR in `patches/postgres-egress.yaml`.
- **A failed migration (`P3009`).** Resolve it with `prisma migrate resolve` from a
  one-off pod using the migrate image, then re-sync.
- **Under Argo CD** a failed Sync hook stops the sync, and Deployments keep the old version.

### ExternalSecret not syncing (prod)

`kubectl describe externalsecret orders-api-secrets` shows the provider error.

- `AccessDenied`: the IRSA role on `external-secrets-reader` lacks access to
  `pulso/prod/<service>`.
- A missing property: the key in Secrets Manager is missing that JSON property.

Running pods keep working on the last Secret, since `deletionPolicy: Retain`. Reloader
restarts the Deployments once the Secret is updated.

### Argo CD shows OutOfSync or Degraded

- **OutOfSync right after a manual `kubectl` change** is self-heal pending. Make the change
  in git.
- **Degraded** usually means a Deployment that cannot become ready. Go to the sections above.
- **A sync blocked outside the prod window.** Run a manual sync if the change is urgent.

### Metrics target down

`PulsoMetricsTargetDown`: Prometheus cannot scrape `/metrics` on orders-api or matching.
These are the only scraped services; support-agents and saga have no exporter yet.

1. Check the target's error in the Prometheus UI (Status → Targets).
2. **Connection refused or timeout:** check that the pod is running and that
   `allow-prometheus-scrape` still allows the `monitoring` namespace and the port.
3. **404:** the service no longer serves `/metrics`. Fix the ServiceMonitor or the service.
4. **`/api/orders/metrics` answers anything but 404 from outside:** the public route lost
   its `(?!metrics(?:/|$))` lookahead (base `ingress.yaml` or the chart). Do not fix it
   with a second Ingress on the same host: ingress-nginx sorts regex locations by length,
   and annotations such as source ranges leak onto the host's default location.
5. **`PulsoMetricsTargetMissing` (no target at all):** Status → Targets has no
   `serviceMonitor/pulso/<service>` entry. Check that the Service still carries
   `app.kubernetes.io/name: <service>`, that its port is still named `http`, and that the
   ServiceMonitor exists in `pulso` (`kubectl -n pulso get servicemonitor`).
6. **Panels empty but the target is up:** the exporter renamed its histogram or status
   label. Update the `metricRelabelings` in `k8s/observability/service-monitors.yaml`.

### 502/503 during deploys

orders-api drains for 5 s and must exit within 25 s, inside a 30 s grace period.

- **5xx at every rollout:** look for `shutdown exceeded` in the logs of terminating pods.
  Long requests or a slow Prisma pool close need a larger `SHUTDOWN_TIMEOUT_MS` *and*
  `terminationGracePeriodSeconds` (the timeout must stay below the grace period).
- **Services without self-draining** (matching, support-agents, saga) rely on the 5 s
  preStop sleep. Increase it if ingress-nginx is slow to drop endpoints.

### Pending / ImagePullBackOff

- **Pending:** `kubectl describe pod`. `Insufficient cpu/memory` means requests are too large
  for the nodes. An unbound PVC on kind means the local-path provisioner is not ready.
- **ImagePullBackOff on kind:** the `:dev` images were never loaded. Run `make kind-load`.

### Connection timeouts between services

A NetworkPolicy is missing for that flow. Every allowed edge is in
`k8s/base/network-policies.yaml`, with the chart's equivalent in `values.yaml`
`networkPolicy` flags. Add the rule in both places. The parity and port-contract tests
fail if they disagree.

### PSA rejection

`violates PodSecurity "restricted:latest"`: the pod spec is missing `runAsNonRoot`,
`seccompProfile`, `capabilities.drop: [ALL]` or `allowPrivilegeEscalation: false`.
`policy_check.py` catches all four before they reach a cluster.
