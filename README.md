# cloud-devops skill

On-demand cloud compute for agents and humans: spawn, list, and terminate CPU/GPU
instances on **AWS EC2** and **Vast.ai**, with mandatory cost approval before
anything that spends money, an interactive CLI, and a local dashboard.

Designed to be installed as an agent **skill**: `SKILL.md` is the contract an
agent reads; each capability is a standalone script under `scripts/` with a
`--json` mode. This repo handles *infra-level* work only — anything repo-specific
(app install, deploy steps) belongs in that repo's own code.

## Install

```bash
./install.sh              # AWS CLI v2 (if missing) + .venv with cloudops AND the vastai CLI
aws configure             # or: aws configure sso && aws sso login
export VAST_API_KEY=...   # optional — https://cloud.vast.ai/account/
```

The Vast.ai backend drives the official **`vastai` CLI** (not Vast's raw REST API,
which returns intermittent 410s during its ongoing v0→v1 migration). `vastai` is a
Python dependency, so `install.sh` pip-installs it **into this skill's own `.venv`**
and the skill always calls that copy — self-contained, never a `vastai` on your PATH.

The installer links `cloudops` and `cloudops-dashboard` into `~/.local/bin`
(adding it to your shell PATH if needed), so they work globally — no venv
activation required. Every operation is a subcommand: `cloudops offers`,
`cloudops spawn`, `cloudops instances`, `cloudops terminate`, … (`cloudops --help`);
bare `cloudops` opens an interactive menu. Agents run it self-contained in one
shell: `source .venv/bin/activate && cloudops <command> …`.

## Layout

```
SKILL.md                     agent contract: providers, commands, cost-approval rules
install.sh                   AWS CLI + venv (incl. vastai CLI) + credential checks
cloudops/                    shared Python package
  providers/base.py          datatypes + Provider interface
  providers/aws.py           EC2 via boto3 (pricing via the AWS Pricing API, cached)
  providers/vast.py          Vast.ai via the official `vastai` CLI (bundled in .venv)
  cli.py                     `cloudops` — subcommand dispatcher + interactive menu
  commands/                  one module per subcommand (spawn, offers, instances, …)
  spawn_flow.py              quote → approval → create; SSH-verify post-spawn helper
  dashboard.py               `cloudops-dashboard` local web dashboard (stdlib, :8787)
scripts/                     legacy thin shims (delegate to cloudops.commands.*)
```

## Cost safety

- `cloudops spawn` **always quotes first**; non-interactive runs must pass `--yes`,
  which agents may only do after the user approves the quoted cost. `--max-hourly`
  is a hard cap on top.
- Everything created is tagged `managed-by=cloudops-skill`; terminating anything
  without that tag requires `--force`.
- Approvals, creations, and terminations are appended to `~/.cloudops/audit.log`.
- AWS pricing lookups are cached a week in `~/.cloudops/cache/`; Cost Explorer
  queries (~$0.01 each) are cached 10 min by the dashboard.

## Adding a provider

Implement `Provider` from `cloudops/providers/base.py` (six methods:
`list_instances`, `list_offers`, `quote`, `spawn`, `terminate`, `usage`), register
it in `cloudops/providers/__init__.py`, and document auth in `SKILL.md`. Everything
else — scripts, CLI, dashboard — picks it up automatically.
