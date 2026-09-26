# OSWorld

[OSWorld](https://github.com/xlang-ai/OSWorld-V2) is a benchmark of real desktop tasks, each run
in an Ubuntu VM and scored by OSWorld's own checks. jev runs there as an OSWorld agent,
`JevAgent` in `typesafe_computer_use/osworld/`, and OSWorld's own GPT agent runs the same task
with GPT-6 Luna to compare against. OSWorld's runner does the reset, the steps, the scoring, and
the recording. Both agents get the same task, 50 steps, and 2 seconds after each action.

OSWorld's VM runs under QEMU and needs a Linux host with KVM, so it does not run on a Mac.
[Run in Google Cloud](#run-in-google-cloud) sets one up.

## Run one task

```
scripts/osworld setup                                    # OSWorld at the pinned commit, jev beside it
scripts/osworld run-jev chrome/<task id> --ocr rapidocr  # one OSWorld 1.0 task with jev
scripts/osworld run-luna chrome/<task id>                # the same task with OSWorld's GPT agent
scripts/osworld results                                  # each task's score, steps, time, and jev's tokens
```

`setup` is the only step between a fresh clone and a run. It fetches OSWorld-V2 at the commit
pinned in `scripts/osworld` into `.osworld/OSWorld-V2`, installs OSWorld's locked dependencies
with its `full` extra into its own `.venv`, installs jev into that `.venv`, and copies
`osworld_overlay/` over the checkout: `mm_agents/jev_agent.py`, and
`scripts/python/run_multienv_jev.py`, OSWorld's generic runner changed only to build `JevAgent`.
With `OSWORLD_OCR=rapidocr` it installs jev's RapidOCR extra too. `setup --v2-tasks` also
downloads OSWorld 2.0's tasks, a gated Hugging Face dataset, and needs `HF_TOKEN`; a 2.0 task is
`tasks/<id>`.

A run reads its keys from `.env`: `TYPESAFE_API_KEY` and the writer's settings for jev,
`OPENAI_API_KEY` for Luna. `--ocr` is required, since a result depends on the OCR that read the
screen: `rapidocr` is the benchmark backend, and `vision` is macOS's own and runs only there.
`OSWORLD_PROVIDER` picks OSWorld's VM provider, `docker` by default. Both runs keep OSWorld's
screen recording and its VNC server on, so the VM can be watched live. Every command prints what
it runs.

## Results

Results land where OSWorld's runner puts them,
`results/pyautogui/<observation type>/<model>/<domain>/<task id>/`: `result.txt` with the score,
`traj.jsonl` with every action, a screenshot per step, and `recording.mp4`. jev's usual run folder
is inside, as `jev/`, so `clicker --image` replays any step; its `run.json` holds the tokens per
model and the OCR backend, provider, and architecture the run used, and `results` shows them. jev
reads `screenshot_a11y_tree` observations and Luna the GPT script's default, `screenshot`, so
compare their times with that in mind. OSWorld's runner skips a task that already has a result,
so a rerun first moves the earlier one to `results/archive/<time>/`.

## Taking an OSWorld update

To take an OSWorld update, change `OSWORLD_COMMIT` in `scripts/osworld`, and `OSWORLD_RELEASE`
with it, the benchmark release that commit names. Copy OSWorld's `scripts/python/run_multienv.py`
at that commit over `osworld_overlay/scripts/python/run_multienv_jev.py` and redo the changes
marked `jev:`, as its header says; a test fails until its header names the new commit. Then run
`scripts/osworld setup`.

## Run in Google Cloud

OSWorld runs each task in an Ubuntu VM under KVM, so it needs a Linux host with KVM, which a Mac
is not. `infra/gcp` describes one such machine on Compute Engine, and `scripts/osworld-gcp` drives
it from here: it starts the machine, syncs your working copy to it, runs the task there with the
output streamed to your terminal, and copies the results back into `results/`. A local edit
applies to the next run with no commit. Locally it needs only `terraform`, `gcloud`, and `rsync`.

### What you need

- Terraform 1.9 or later, and the gcloud CLI signed in twice: `gcloud auth login` for SSH, and
  `gcloud auth application-default login` for Terraform.
- A Google Cloud project with billing and the Compute Engine API on, where nested virtualization is
  allowed: the organization policy `constraints/compute.disableNestedVirtualization` must not be
  enforced.
- To create the machine (`up`, `down`): the Editor role, since Terraform also makes a firewall rule,
  a Cloud Router and NAT, and turns APIs on. For the nightly stop it also lets Compute Engine's
  service agent stop the machine, which takes permission to change the project's IAM policy; Owner
  has both. Without that permission, set `grant_schedule_permission = false` and have an
  administrator grant `roles/compute.instanceAdmin.v1` to
  `service-PROJECT_NUMBER@compute-system.iam.gserviceaccount.com`, or set `nightly_stop_hour = null`.
- To use it (every other command): Editor or Compute Instance Admin (v1). Both include OS Admin
  Login, which the script needs to act as the machine's `osworld` user.
- With `ssh = "iap"` (the default), also the IAP-secured Tunnel User role
  (`roles/iap.tunnelResourceAccessor`), which Editor does not include. Terraform turns the IAP API
  on.
- For the optional budget, the Billing Account Costs Manager role on the billing account.

### Commands

```
cp infra/gcp/terraform.tfvars.example infra/gcp/terraform.tfvars   # set project, region, zone
scripts/osworld-gcp up                                    # terraform apply; the machine sets itself up
scripts/osworld-gcp run-jev chrome/<id> --ocr rapidocr    # start, sync, run, copy the results back
scripts/osworld-gcp run-luna chrome/<id>                  # the same with OSWorld's GPT agent
scripts/osworld-gcp watch                                 # during a run: the task VM's screen, in a browser
scripts/osworld-gcp status                                # running or stopped, and since when
scripts/osworld-gcp stop                                  # stop now; the disk stays
scripts/osworld-gcp pull-results                          # copy every result back, after an interrupted run
scripts/osworld-gcp ssh                                   # a shell on the machine, for debugging
scripts/osworld-gcp down                                  # destroy everything infra/gcp made
```

`--dry-run` before any command prints each command it would run and runs none. `up` caches the
machine's project, zone, and name in `.osworld/gcp.json`, so the other commands need no Terraform.

### The machine

The machine is long-lived: a run starts it when it is stopped, and waits for its startup script.
The first boot installs Docker, uv, this repo, and OSWorld (`scripts/osworld setup`, with
`OSWORLD_PROVIDER=docker` and the `rapidocr` extra), which takes several minutes; later boots only
check, and a run starts in about a minute. On the machine, the repo is `/opt/typesafe-computer-use`,
owned by an unprivileged `osworld` user, and the startup log is `/var/log/osworld-startup.log`.

SSH has two modes, set by `ssh` in `terraform.tfvars`:

- `iap` (default): no external IP and no open ports. Port 22 accepts only Google's IAP range, and
  the machine reaches the internet through a Cloud NAT that Terraform creates for its subnetwork
  (`create_nat = false` when the network has one already).
- `external_ip`: an external IP, with port 22 open to `ssh_source_cidr`, for projects where IAP is
  not granted. Many projects' `default` network also has a `default-allow-ssh` rule open to every
  address, which this mode does not remove; check the network's firewall rules.

Either way, login goes through OS Login. Your `.env` is copied over SSH before each run into a file
only the `osworld` user can read (mode 600), and is never printed. It never enters Terraform state,
and the machine has no service account, so it holds no Google Cloud credentials.

### Cost guards and cost

Three guards keep a forgotten machine from running up a bill, all on by default:

| guard | what it does | setting |
|---|---|---|
| idle shutdown | a timer on the machine checks every 5 minutes and powers it off once no `run_multienv` process has run and no SSH connection has been open for that long | `idle_shutdown_minutes` (60; 0 turns it off) |
| nightly stop | an instance schedule stops the machine every day | `nightly_stop_hour` (2) in `time_zone` (`Etc/UTC`); `null` turns it off |
| budget | email alerts to the billing account's administrators at 50, 90, and 100 percent of a monthly budget on this machine's cost | `billing_account` (empty: no budget), `budget_usd` (50) |

The machine is also Spot by default (`spot = true`): Google may stop it at any time, which costs
only a rerun of the task. Every resource that takes labels carries `app = "typesafe-computer-use"`
and `purpose = "osworld"`, so a shared project can find and bill them.

Cost, for the default `n2-standard-8` (check current prices for your region): about $0.17 an hour
Spot, about $0.31 to $0.39 an hour on demand, and about $10 a month for the 150 GB disk, stopped or
not. Cloud NAT adds a little per running hour and about $0.045 per GB it carries, most of it the
one-time download of OSWorld's VM image.
