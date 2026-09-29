# OSWorld

[OSWorld](https://github.com/xlang-ai/OSWorld-V2) is a benchmark of real desktop tasks, each run
in an Ubuntu VM and scored by OSWorld's own checks. jev runs there as an OSWorld agent,
`JevAgent` in `typesafe_computer_use/osworld/`, and OSWorld's own GPT agent runs the same task
with GPT-6 Luna to compare against. OSWorld's runner does the reset, the steps, the scoring, and
the recording. Both agents get the same task, 50 steps, and no wait after each action:
`SLEEP_AFTER_EXECUTION` in `scripts/osworld` is 0, where runs up to 2026-09-29 had 2 seconds, and
each benchmark row records it ([Results](#results)). Each agent waits only when it chooses to,
with a sleep in the VM: Luna for as long as it asks, a second by default, and jev for 0.5 s,
when what the goal needs is not on the screen yet, before it looks again. Neither uses OSWorld's
`WAIT` action, which sleeps the fixed pause. With no pause, OSWorld's screenshot can come before
Chrome has drawn what an action did, so jev looks once more, the same 0.5 s later, when the screen
seems unchanged, or when the capture lacks many of the controls its tree has
([Stalls](how-a-step-works.md#stalls)). Each such look is an OSWorld step of its own.

OSWorld's VM runs under QEMU and needs a Linux host with KVM, so it does not run on a Mac.
[Run in Google Cloud](#run-in-google-cloud) sets one up.

## Run one task

```
scripts/osworld setup                                    # OSWorld at the pinned commit, jev beside it
scripts/osworld run-jev chrome/<task id> --ocr rapidocr  # one OSWorld 1.0 task with jev
scripts/osworld run-jev chrome/<id> chrome/<id> --ocr rapidocr   # several, one after another
scripts/osworld run-jev chrome/<id> chrome/<id> chrome/<id> --ocr rapidocr --envs 3   # three VMs side by side
scripts/osworld run-luna chrome/<task id>                # the same with OSWorld's GPT agent on Luna
scripts/osworld results                                  # each task's score, steps, time, and tokens, then each agent's means
```

`setup` is the only step between a fresh clone and a run. It fetches OSWorld-V2 at the commit
pinned in `scripts/osworld` into `.osworld/OSWorld-V2`, installs OSWorld's locked dependencies
with its `full` extra into its own `.venv`, installs jev into that `.venv`, and copies
`osworld_overlay/` over the checkout. For jev, that is `mm_agents/jev_agent.py` and
`scripts/python/run_multienv_jev.py`, OSWorld's generic runner changed only to build `JevAgent`,
to leave AWS's image map to the AWS provider, to leave OSWorld's proxy off, and to hand jev the
VM's controller. That proxy needs credentials of OSWorld's own, and without them every task marked
`"proxy": true` loads no page. The controller lets jev fetch the accessibility tree itself, so
OSWorld's observation carries the screenshot alone ([The accessibility tree](#the-accessibility-tree)).
For Luna, it is `mm_agents/luna_agent.py` and `scripts/python/run_multienv_luna.py`, OSWorld's GPT
runner changed only to leave the proxy off too and to write each task's tokens to `usage.json`,
since OSWorld's GPT agent records none. Its agent is OSWorld's own, counting each reply's usage and
nothing else. Luna runs at the GPT runner's default reasoning effort, `xhigh`, which sets its
accuracy and cost more than anything else; `OSWORLD_LUNA_EFFORT` picks another, and `usage.json`
records it. Every run copies the overlay again, so an edit to it needs no second `setup`.
With `OSWORLD_OCR=rapidocr` it installs jev's RapidOCR extra too. `setup --v2-tasks` also
downloads OSWorld 2.0's tasks, a gated Hugging Face dataset, and needs `HF_TOKEN`; a 2.0 task is
`tasks/<id>`.

A run reads its keys from `.env`: `TYPESAFE_API_KEY` for jev and `OPENAI_API_KEY` for both. jev's
writer and answer model in OSWorld are Luna too, so a run compares jev with Luna inside it to Luna
alone: `run-jev` points the `CLICKER_WRITER_*` settings at OpenAI's API and `gpt-6-luna`, with no
reasoning for the writer's short calls and `low` for the answer model. `.env` overrides any of them. `--ocr` is required, since a result depends on the OCR that read the
screen: `rapidocr` is the benchmark backend, and `vision` is macOS's own and runs only there.
`OSWORLD_PROVIDER` picks OSWorld's VM provider, `docker` by default. Both runs keep OSWorld's
screen recording and its VNC server on, so the VM can be watched live. Every command prints what
it runs.

## Results

Results land where OSWorld's runner puts them,
`results/pyautogui/<observation type>/<model>/<domain>/<task id>/`: `result.txt` with the score,
`traj.jsonl` with every action, a screenshot per step, and `recording.mp4`. jev's usual run folder
is inside, as `jev/`, so `clicker --image` replays any step; its `run.json` holds the tokens per
model and the OCR backend, provider, and architecture the run used, and `results` shows them, as
it shows Luna's tokens from `usage.json`, reasoning tokens among them, with its model time and
reasoning effort. jev's results are filed under `screenshot_a11y_tree`, since it reads the tree, and
Luna's under the GPT script's default, `screenshot`; OSWorld's observations carry the screenshot
alone for both, and jev fetches its tree itself. After the tasks, `results` sums up each
agent: its means over the tasks it solved and, apart, over those it failed (a score below 1), since
a run stuck until the step limit skews a mean over both, and the median time a failed task took to
end. A task with no score is listed there, not counted. OSWorld's runner skips a task that already
has a result, so a rerun first moves the earlier one to `results/archive/<time>/`;
`scripts/osworld-gcp` moves its local copy aside the same way before a run, so a pull never mixes
two runs' files.

After each cloud run, `scripts/osworld-gcp` appends one JSON line per task to
`benchmarks/osworld/<run>.jsonl`: the score, steps, time, outcome, and tokens per model (for Luna,
also its model time and reasoning effort; for jev, where its trees came from and how many fell back
to OSWorld's fetch), with the commit the run synced, whether the synced copy
differed from it (and a hash of the difference), OSWorld's pinned commit, the wait after each action
(`sleep_after_execution`; a row without it waited 2 s), the command, and the machine. Rows are never
edited; a rerun is a new file. They are not committed for you: committing a run's file is what makes
it part of the record, and a row from a dirty copy says so. A pull the tunnel drops is tried again;
when the results still do not come back, the run records no rows, which would say that no task came
back, and prints the command that records them after `pull-results`.
Screenshots and recordings stay in `results/`, out of git.

OSWorld keeps no accessibility tree. With `JEV_OSWORLD_SAVE_A11Y=1` (in the environment or `.env`),
jev saves each observation's raw tree, the one it read, in its run folder as `obs-NNN-a11y.xml`,
counting from `000`, the task's first, and OSWorld's full tree of the same screen beside it as
`obs-NNN-a11y-full.xml` when a check fetched one; that is what a mismatch between the tree and
`osworld/a11y.py` is diagnosed from. `tests/fixtures/osworld/` holds trees captured that way.

## The accessibility tree

OSWorld's own tree fetch walks every application on the desktop, about 2,500 nodes on a Chrome task,
2,100 of them GNOME Shell's, asks each node about ten questions over D-Bus, and runs
`libreoffice --version` first: 2.4 s of every step, most of a step's time besides OSWorld's wait, 2 s then.
jev reads one application of that tree. So jev's runner asks OSWorld for the screenshot alone, and
jev fetches the tree itself when each observation comes (`osworld/tree.py`): `osworld/light_walk.py`
runs in the VM through OSWorld's `/run_python` endpoint, finds the application in front as
`osworld/a11y.py` does, walks it alone, and asks each node only what `a11y.py` reads. It prints the
XML OSWorld's fetch writes, so `a11y.py` is the one parser of both. On 52 trees saved from
OSWorld's Chrome tasks, jev read the same app, window, focused field, URL, and controls with their
boxes off both, from a sixth of the nodes and about a tenth of the AT-SPI calls;
`tests/test_osworld_tree.py` checks that on the trees in `tests/fixtures/osworld/`.

The fetch runs on a thread of its own while the step's OCR reads the screenshot, and only a read of
the tree waits for it. The OCR bets on the previous step's app and window, which a page in one
window keeps, and the step reads the screen again itself when the tree says otherwise
(`OcrCache.read_ahead` in `perception.py`). No fetch runs while the VM acts: the step's actions go
to OSWorld only once the fetch before them is over. OSWorld's wait and its `env.step` for every
action stay as they were, so the tree's time moves from OSWorld's step into jev's own.

When the light walk fails, takes longer than 10 s, or leaves the tree to OSWorld (a spreadsheet,
whose cells OSWorld's walk picks out by hand, or a runaway of more than 20,000 nodes), OSWorld's own
fetch stands in, and a warning in OSWorld's log says why. That fetch asks with no timeout, and on
chrome/2ad9387a, after a click on Chrome's menu, it never came back and held the run up for good. So
each request runs on a thread of its own with a deadline, 10 s for the light walk and 20 s for
OSWorld's fetch, and when both miss, the step goes on with no tree: the app and the controls are
unknown, and OCR still reads the screen. jev's `run.json` records it all under `osworld`: `tree` is
`jev-light`, `tree_fetches` counts the fetches, `tree_fallbacks` those OSWorld's stood in for, with
the reasons, `tree_missing` those that brought no tree at all, and `tree_seconds` and `tree_nodes`
give each one's mean and maximum.

`JEV_OSWORLD_TREE_CHECK=1` checks each light tree against OSWorld's on the same screen: after each
light walk it fetches OSWorld's tree and walks again, and records under `tree_check` how many
screens held still across the two walks, on how many of those jev read the two trees alike, where
they parted, and what each fetch cost. `tree` is then `jev-light-checked`, since every step also
paid for OSWorld's fetch: such a run checks the walk, and its times are no benchmark.

## Taking an OSWorld update

To take an OSWorld update, change `OSWORLD_COMMIT` in `scripts/osworld`, and `OSWORLD_RELEASE`
with it, the benchmark release that commit names. Copy OSWorld's `scripts/python/run_multienv.py`
at that commit over `osworld_overlay/scripts/python/run_multienv_jev.py`, and
`scripts/python/run_multienv_gpt_response_api.py` over `run_multienv_luna.py` beside it, and redo
the changes marked `jev:`, as each header says; a test fails until each header names the new
commit. Then run `scripts/osworld setup`.

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
scripts/osworld-gcp pull-results                          # copy every result back, after an interrupted run; starts the machine
scripts/osworld-gcp ssh [-- COMMAND]                      # a shell on the machine, or one command
scripts/osworld-gcp down                                  # destroy everything infra/gcp made
```

`--dry-run` before any command prints each command it would run and runs none. `up` caches the
machine's project, zone, and name in `.osworld/gcp.json`, so the other commands need no Terraform.

### The machine

The machine is long-lived: a run starts it when it is stopped, and waits for its startup script.
The first boot installs Docker, uv, a C toolchain with the kernel and Python headers (OSWorld's lock
builds a few packages from source), this repo, and OSWorld (`scripts/osworld setup`, with
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
only a rerun of the tasks that had not finished. A run the machine stops under ends at once: the
script starts the machine again, brings back the tasks that finished, and records them, and a task
that did not finish has no score in its row. Every resource that takes labels carries `app = "typesafe-computer-use"`
and `purpose = "osworld"`, so a shared project can find and bill them.

Cost, for the default `n2-standard-8` (check current prices for your region): about $0.17 an hour
Spot, about $0.31 to $0.39 an hour on demand, and about $10 a month for the 150 GB disk, stopped or
not. Cloud NAT adds a little per running hour and about $0.045 per GB it carries, most of it the
one-time download of OSWorld's VM image.
