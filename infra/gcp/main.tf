# One Linux machine on Compute Engine that runs OSWorld's Docker provider: nested virtualization
# for the task VM's KVM, Docker, and this repo, set up by startup.sh on every boot.
# scripts/osworld-gcp drives it; see "Run in Google Cloud" in the README.
#
# Three guards keep a forgotten machine from running up a bill: startup.sh powers it off when
# idle, a schedule stops it every night, and an optional budget sends alerts.

terraform {
  # 1.9 lets a variable's validation read another variable.
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 8.4.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "= 0.14.2"
    }
  }
}

locals {
  # On every resource that takes labels, so a shared project can find and bill them.
  labels = {
    app     = "typesafe-computer-use"
    purpose = "osworld"
  }

  iap          = var.ssh == "iap"
  nat          = local.iap && var.create_nat
  nightly_stop = var.nightly_stop_hour != null
  budget       = var.billing_account != ""
  ssh_tag      = "${var.instance_name}-ssh"

  # Google's range for IAP TCP forwarding.
  iap_range = "35.235.240.0/20"
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone

  # The budget API bills each call to a project, which user credentials do not name on their own.
  user_project_override = true
  billing_project       = var.project

  default_labels = local.labels
}

data "google_project" "this" {}

data "google_compute_subnetwork" "this" {
  name   = coalesce(var.subnetwork, var.network)
  region = var.region
}

resource "google_project_service" "apis" {
  for_each = toset(concat(
    ["oslogin.googleapis.com"],
    local.iap ? ["iap.googleapis.com"] : [],
    local.budget ? ["billingbudgets.googleapis.com"] : [],
  ))

  service = each.value
  # Other things in the project may use these APIs; `down` leaves them on.
  disable_on_destroy = false
}

# SSH only: from IAP's range with ssh = iap, from ssh_source_cidr with ssh = external_ip.
resource "google_compute_firewall" "ssh" {
  name        = "${var.instance_name}-ssh"
  network     = data.google_compute_subnetwork.this.network
  description = "SSH to the OSWorld machine (typesafe-computer-use)."
  direction   = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = [local.iap ? local.iap_range : var.ssh_source_cidr]
  target_tags   = [local.ssh_tag]
}

# With no external IP, the machine reaches the internet (apt, Docker Hub, GitHub, Hugging Face,
# model APIs) through Cloud NAT, for its own subnetwork only.
resource "google_compute_router" "nat" {
  count = local.nat ? 1 : 0

  name    = "${var.instance_name}-router"
  region  = var.region
  network = data.google_compute_subnetwork.this.network
}

resource "google_compute_router_nat" "nat" {
  count = local.nat ? 1 : 0

  name                               = "${var.instance_name}-nat"
  router                             = google_compute_router.nat[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = data.google_compute_subnetwork.this.self_link
    source_ip_ranges_to_nat = ["PRIMARY_IP_RANGE"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Guard 2: stop the machine every night, so a forgotten one never runs all month.
resource "google_compute_resource_policy" "nightly_stop" {
  count = local.nightly_stop ? 1 : 0

  name        = "${var.instance_name}-nightly-stop"
  region      = var.region
  description = "Stops the OSWorld machine every night (typesafe-computer-use)."

  instance_schedule_policy {
    vm_stop_schedule {
      schedule = "0 ${coalesce(var.nightly_stop_hour, 0)} * * *"
    }
    time_zone = var.time_zone
  }
}

# A schedule stops the machine as Compute Engine's service agent, and attaching one fails unless
# that agent may stop instances.
resource "google_project_iam_member" "schedule_agent" {
  count = local.nightly_stop && var.grant_schedule_permission ? 1 : 0

  project = var.project
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:service-${data.google_project.this.number}@compute-system.iam.gserviceaccount.com"
}

# A new IAM grant takes a minute or so to apply everywhere.
resource "time_sleep" "schedule_agent" {
  count = length(google_project_iam_member.schedule_agent)

  create_duration = "90s"
  depends_on      = [google_project_iam_member.schedule_agent]
}

resource "google_compute_instance" "osworld" {
  name         = var.instance_name
  zone         = var.zone
  machine_type = var.machine_type
  description  = "Runs OSWorld tasks with its Docker provider (typesafe-computer-use)."
  tags         = [local.ssh_tag]
  labels       = local.labels

  # A change of machine type stops and restarts the machine instead of failing.
  allow_stopping_for_update = true

  boot_disk {
    auto_delete = true

    initialize_params {
      image  = var.image
      size   = var.disk_gb
      type   = "pd-balanced"
      labels = local.labels
    }
  }

  network_interface {
    subnetwork = data.google_compute_subnetwork.this.self_link

    dynamic "access_config" {
      for_each = local.iap ? [] : [1]
      content {}
    }
  }

  # OSWorld's task VM runs under KVM inside Docker.
  advanced_machine_features {
    enable_nested_virtualization = true
  }

  scheduling {
    provisioning_model          = var.spot ? "SPOT" : "STANDARD"
    preemptible                 = var.spot
    automatic_restart           = !var.spot
    on_host_maintenance         = var.spot ? "TERMINATE" : "MIGRATE"
    instance_termination_action = var.spot ? "STOP" : null
  }

  # No service account: the machine calls no Google Cloud API, so it holds no credentials.

  metadata = {
    enable-oslogin = "TRUE"
    # Metadata rather than metadata_startup_script, so an edit to startup.sh updates the machine
    # in place (it runs on the next boot) instead of replacing it and its disk.
    startup-script        = file("${path.module}/startup.sh")
    idle-shutdown-minutes = tostring(var.idle_shutdown_minutes)
    repo-url              = var.repo_url
    repo-ref              = var.repo_ref
  }

  resource_policies = google_compute_resource_policy.nightly_stop[*].self_link

  lifecycle {
    # An image family moves on; that must not rebuild the machine and lose its disk.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }

  depends_on = [
    google_project_service.apis,
    google_compute_firewall.ssh,
    google_compute_router_nat.nat,
    time_sleep.schedule_agent,
  ]
}

# Guard 3, optional: a monthly budget on this machine's cost, with email alerts to the billing
# account's administrators.
resource "google_billing_budget" "osworld" {
  count = local.budget ? 1 : 0

  billing_account = var.billing_account
  display_name    = "${var.instance_name} (typesafe-computer-use)"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
    labels   = { purpose = local.labels.purpose }
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.apis]
}

output "project" {
  value = var.project
}

output "zone" {
  value = var.zone
}

output "instance_name" {
  value = google_compute_instance.osworld.name
}

output "ssh" {
  description = "iap or external_ip; scripts/osworld-gcp reads it."
  value       = var.ssh
}

output "ssh_command" {
  description = "A shell on the machine, for debugging. scripts/osworld-gcp ssh runs the same."
  value       = "gcloud compute ssh ${google_compute_instance.osworld.name} --project ${var.project} --zone ${var.zone}${local.iap ? " --tunnel-through-iap" : ""}"
}
