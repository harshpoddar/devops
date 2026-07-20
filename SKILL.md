---
name: cloud-devops
description: >
  Provision and manage on-demand cloud compute: CPU/GPU EC2 instances on AWS and
  rented GPUs on Vast.ai. Use when the user wants to list cloud instances, search
  instance types or GPU offers with prices, spawn or terminate an instance, or
  check cloud spend and balances. Spawning always requires explicit user approval
  of the quoted cost.
---

# Cloud DevOps skill

This skill manages **infra-level** resources: instances, GPUs, pricing, spend.
Repo-level concerns (installing a project, deploying its code onto an instance)
belong in each repo's own scripts — do not put them here.

## Providers

| Provider | Use for | Auth |
|---|---|---|
| `aws` | EC2 CPU + GPU instances, stable/enterprise workloads | Standard AWS credential chain: `aws configure`, `aws sso login`, or env vars. Region from AWS config or `--region`. |
| `vast` | Cheap marketplace GPU rentals (often 3–6x cheaper than AWS for the same GPU) | API key in `VAST_API_KEY` env var, `~/.vast_api_key`, or `~/.config/vastai/vast_api_key`. Get one at https://cloud.vast.ai/account/. SSH uses your local key (`~/.ssh/id_ed25519.pub`), which spawn registers on the account automatically. |

The Vast backend drives the official **`vastai` CLI**, bundled inside this skill's
own `.venv` (installed by `./install.sh`) — not Vast's raw REST API, which returns
intermittent 410s mid v0→v1 migration. The skill always invokes `.venv/bin/vastai`,
so nothing depends on a `vastai` on your PATH.

## Setup (once)

```bash
./install.sh          # AWS CLI v2 (if missing), .venv, pip installs this package + the vastai CLI
aws configure         # or: aws configure sso && aws sso login
export VAST_API_KEY=...   # optional, only for Vast (or ~/.config/vastai/vast_api_key)
```

**Always run scripts with the skill's own interpreter**: `<skill-root>/.venv/bin/python`
(created by `install.sh`). Do not use the system `python3` or another project's venv.
If `.venv` is missing, run `./install.sh` first. Scripts self-check on start: if
dependencies are missing they exit with code **4** and print the exact fix
(run `./install.sh`, or the correct interpreter path to re-run with) — relay that
message to the user or run the installer, then retry.

**The bundled Vast CLI command**: the Vast backend shells out to the official
`vastai` CLI, which `install.sh` installs **into this skill's own venv**. The skill
always calls that copy by absolute path — `<skill-root>/.venv/bin/vastai` — so it
never depends on a `vastai` on the user's PATH. For manual/advanced Vast operations
not exposed by the scripts, invoke it directly the same way, e.g.:

```bash
<skill-root>/.venv/bin/vastai show instances --raw
<skill-root>/.venv/bin/vastai search offers 'gpu_name=RTX_4090 num_gpus=1' -o dph_total --raw
```

(It reads the API key from `~/.config/vastai/vast_api_key`, or pass `--api-key`.)

## Scripts

Every script accepts `--json` (machine-readable output — prefer it when you are
an agent) and, where it applies, `--provider aws|vast|all` and `--region`.
A provider with missing credentials is reported in the `errors` field without
failing the others.

### 1. List instances

```bash
python scripts/list_instances/list_instances.py [--provider all] [--region us-east-1] [--json]
```

Returns id, name, status, type, region, IP/SSH endpoint, $/hr, whether the
instance is `managed` (created by this skill — AWS tag `managed-by=cloudops-skill`),
and for Vast a `ports` map showing how exposed container ports map to public
host ports. Use this whenever the user asks what's running or needs an instance id.

### 2. List offers — instance types / GPU offers with pricing, filterable

```bash
python scripts/list_offers/list_offers.py [--provider all] \
  [--gpus 1] [--gpu-type "A100"] [--cuda 12.8] [--min-vcpus 8] [--min-memory 32] \
  [--max-hourly 1.50] [--limit 15] [--json]
```

Cheapest first. Vast offer IDs churn — treat them as valid for minutes, not hours.
Vast results only include verified, currently-rentable, not-already-rented offers
(suspiciously cheap offers outside these filters are usually not actually rentable).
`--cuda VER` keeps only hosts whose CUDA is ≥ VER (Vast only; each offer's CUDA
shows in the Notes column / `extra.cuda`). First AWS run is slow (pricing API);
results are cached for a week in `~/.cloudops/cache/`.

### 3. Spawn an instance — ⚠ costs money, approval is mandatory

**Agent contract — never skip this:**

1. Run with `--quote` first. It prices the spawn and creates **nothing**.
2. Show the user the hourly + monthly cost and ask for approval.
3. Only after the user explicitly approves **in this conversation**, re-run the
   identical command with `--yes` instead of `--quote`. Never pass `--yes` on
   your own initiative; non-interactive runs without `--yes` exit code 3 by design.

```bash
# AWS
python scripts/spawn_instance/spawn_instance.py --provider aws --type g5.xlarge \
  [--region us-east-1] [--ami ami-...] [--disk 100] [--key-name my-key] \
  [--security-group sg-...] [--open-port 8888] [--name train-run-1] [--ttl-hours 8] --quote

# Vast (pick an --offer-id from list_offers, or auto-pick cheapest by GPU)
python scripts/spawn_instance/spawn_instance.py --provider vast \
  --gpu-type "RTX 4090" --gpus 1 [--cuda 12.8] [--image pytorch/pytorch:latest] \
  [--disk 40] [--open-port 8888] [--ssh-key ~/.ssh/id_ed25519.pub] \
  [--ssh-wait-timeout 720] [--no-ssh-wait] --quote
```

Vast CUDA: if the workload/image needs a minimum CUDA version, pass `--cuda VER` —
it constrains auto-picks AND rejects an explicit `--offer-id` whose host is below it.
The quote's `cuda` field shows the picked host's version: confirm it meets the
requirement before asking the user to approve.

Ports: `--open-port N` (repeatable) exposes TCP ports. On AWS it creates a
dedicated, tagged security group **open to 0.0.0.0/0** (tell the user; port 22
is auto-added when --key-name is given and no explicit --security-group);
`--security-group sg-...` attaches existing groups for finer control. On Vast
each exposed port is mapped to a **random public host port** — read the actual
mapping from the `ports` field of list_instances --json once running.

Vast SSH — automatic key setup + self-check (this is the fix for the old
"Permission denied (publickey)" flakiness): after creating, spawn registers your
local public key on the Vast **account** (so Vast injects it at container boot —
the reliable path) and attaches it to the instance, then **polls an actual SSH
login** and only reports success once it works. The result includes the exact
`ssh -i <key> -p <port> root@<host>` command to hand the user.
- `--ssh-key PATH` — public key to use (default `~/.ssh/id_ed25519.pub`, then `id_rsa.pub`).
- `--ssh-wait-timeout SEC` — how long to wait for login (default **720s**; Vast key
  injection can lag several minutes, so **wait — do not destroy** on the first denials).
- `--no-ssh-wait` — skip the check and report as soon as created.
- On timeout the instance is **kept** (it bills) and reported with the ssh command +
  a "key still propagating, retry shortly" note — never auto-destroyed.
- Needs a local SSH keypair; if none exists, run `ssh-keygen -t ed25519` first.

Guards and behavior:
- `--max-hourly USD` aborts (exit 2) if the quote exceeds it — use it as a belt-and-braces cap.
- Exit codes: `0` created, `2` cost guard exceeded, `3` approval missing or denied.
- AWS AMI default: Amazon Linux 2023; GPU types get the Deep Learning Base GPU AMI
  when available. Without `--key-name` there is no SSH — say so to the user.
- Everything spawned is tagged `managed-by=cloudops-skill`; approvals and creations
  are appended to `~/.cloudops/audit.log`.
- After spawning, always remind the user the instance **bills until terminated**.

### 4. Start / stop an instance

```bash
python scripts/start_instance/start_instance.py --provider vast --id 12345 [--yes] [--json]
python scripts/stop_instance/stop_instance.py  --provider vast --id 12345 [--yes] [--json]
```

Confirmation required (get the user's OK, then `--yes`); unmanaged AWS instances
need `--force`. Cost facts to tell the user: stopping halts compute/GPU billing
but **storage keeps billing** on both providers; on Vast a restart is **not
guaranteed** — the host may rent the GPUs to someone else while stopped. If a
Vast start fails with no capacity, offer clone_instance instead.

### 5. Clone an instance

```bash
python scripts/clone_instance/clone_instance.py --provider vast --id 12345 \
  [--with-data] [--data-path /workspace] [--offer-id N] [--cuda VER] [--name X] \
  [--disk GB] [--open-port N] [--ssh-key PATH] [--ssh-wait-timeout SEC] \
  [--no-ssh-wait] [--max-hourly USD] --quote
```

Default: recreates the instance's **configuration** (GPU model/count, image,
disk size, onstart / AMI, type, security groups) — disk contents NOT copied.
Vast clones also require the new host's CUDA ≥ the source host's, so the same
image keeps working; `--cuda VER` overrides that floor (up or down).
`--with-data` makes it a replica: AWS snapshots the source into an AMI first
(minutes; AMI + snapshots bill storage until deregistered; no source downtime
unless `--reboot-source`); Vast waits for the clone to boot then rsyncs
`--data-path` (default `/workspace`) source→clone on Vast's side. Creates a
billed instance, so the spawn contract applies unchanged: `--quote` → user
approval → re-run with `--yes`. Same exit codes as spawn. The Vast SSH
self-check (see spawn) runs on the clone too — for `--with-data`, SSH is
verified first, then the data copy starts (`--ssh-key`/`--ssh-wait-timeout`/
`--no-ssh-wait` apply).

### 6. Terminate an instance

```bash
python scripts/terminate_instance/terminate_instance.py --provider aws --id i-0abc... [--yes] [--json]
```

Confirmation required (same rule as spawning: get the user's OK, then `--yes`).
AWS instances **not** tagged `managed-by=cloudops-skill` additionally need `--force`.

### 7. Account metrics

```bash
python scripts/account_metrics/account_metrics.py [--provider all] [--json]
```

AWS: month-to-date spend by service (Cost Explorer — each query costs ~$0.01),
running-instance burn rate. Vast: prepaid credit balance, burn rate, instance counts.

## Human tools (mention these to the user, don't drive them yourself)

- `cloudops` — interactive terminal CLI: overall usage + instance details as tables.
- `cloudops-dashboard` — read-only local web dashboard at http://127.0.0.1:8787
  (stat tiles for running instances / burn rate / month-to-date spend / Vast balance,
  plus a live instance table).
- `<skill-root>/.venv/bin/vastai` — the bundled official Vast.ai CLI, for any raw
  Vast operation the scripts above don't cover (`vastai --help`). Same venv, same
  API key; the skill uses this exact binary internally.

## Cost-safety rules for agents

1. Quote before spawning, every time. Approval must come from the user, not you.
2. Prefer `--max-hourly` as an extra cap even after approval.
3. When work is done, offer to terminate what you spawned (`managed: true` instances).
4. Never terminate unmanaged AWS instances without the user naming them explicitly.
