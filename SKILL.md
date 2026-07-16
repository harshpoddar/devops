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
| `vast` | Cheap marketplace GPU rentals (often 3–6x cheaper than AWS for the same GPU) | API key in `VAST_API_KEY` env var or `~/.vast_api_key`. Get one at https://cloud.vast.ai/account/. SSH uses the key registered on that same page. |

## Setup (once)

```bash
./install.sh          # installs AWS CLI v2 if missing, creates .venv, pip installs this package
aws configure         # or: aws configure sso && aws sso login
export VAST_API_KEY=...   # optional, only for Vast
```

**Always run scripts with the skill's own interpreter**: `<skill-root>/.venv/bin/python`
(created by `install.sh`). Do not use the system `python3` or another project's venv.
If `.venv` is missing, run `./install.sh` first. Scripts self-check on start: if
dependencies are missing they exit with code **4** and print the exact fix
(run `./install.sh`, or the correct interpreter path to re-run with) — relay that
message to the user or run the installer, then retry.

## Scripts

Every script accepts `--json` (machine-readable output — prefer it when you are
an agent) and, where it applies, `--provider aws|vast|all` and `--region`.
A provider with missing credentials is reported in the `errors` field without
failing the others.

### 1. List instances

```bash
python scripts/list_instances/list_instances.py [--provider all] [--region us-east-1] [--json]
```

Returns id, name, status, type, region, IP/SSH endpoint, $/hr, and whether the
instance is `managed` (created by this skill — AWS tag `managed-by=cloudops-skill`).

### 2. List offers — instance types / GPU offers with pricing, filterable

```bash
python scripts/list_offers/list_offers.py [--provider all] \
  [--gpus 1] [--gpu-type "A100"] [--min-vcpus 8] [--min-memory 32] \
  [--max-hourly 1.50] [--limit 15] [--json]
```

Cheapest first. Vast offer IDs churn — treat them as valid for minutes, not hours.
First AWS run is slow (pricing API); results are cached for a week in `~/.cloudops/cache/`.

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
  [--security-group sg-...] [--name train-run-1] [--ttl-hours 8] --quote

# Vast (pick an --offer-id from list_offers, or auto-pick cheapest by GPU)
python scripts/spawn_instance/spawn_instance.py --provider vast \
  --gpu-type "RTX 4090" --gpus 1 [--image pytorch/pytorch:latest] [--disk 40] --quote
```

Guards and behavior:
- `--max-hourly USD` aborts (exit 2) if the quote exceeds it — use it as a belt-and-braces cap.
- Exit codes: `0` created, `2` cost guard exceeded, `3` approval missing or denied.
- AWS AMI default: Amazon Linux 2023; GPU types get the Deep Learning Base GPU AMI
  when available. Without `--key-name` there is no SSH — say so to the user.
- Everything spawned is tagged `managed-by=cloudops-skill`; approvals and creations
  are appended to `~/.cloudops/audit.log`.
- After spawning, always remind the user the instance **bills until terminated**.

### 4. Terminate an instance

```bash
python scripts/terminate_instance/terminate_instance.py --provider aws --id i-0abc... [--yes] [--json]
```

Confirmation required (same rule as spawning: get the user's OK, then `--yes`).
AWS instances **not** tagged `managed-by=cloudops-skill` additionally need `--force`.

### 5. Account metrics

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

## Cost-safety rules for agents

1. Quote before spawning, every time. Approval must come from the user, not you.
2. Prefer `--max-hourly` as an extra cap even after approval.
3. When work is done, offer to terminate what you spawned (`managed: true` instances).
4. Never terminate unmanaged AWS instances without the user naming them explicitly.
