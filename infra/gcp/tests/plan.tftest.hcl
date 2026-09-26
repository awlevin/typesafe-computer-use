# Offline: mock providers stand in for Google Cloud, so `terraform test` needs no credentials and
# creates nothing. Each run plans with some variables and checks what the plan would make.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
  mock_data "google_compute_subnetwork" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/example-project/regions/us-central1/subnetworks/default"
      network   = "https://www.googleapis.com/compute/v1/projects/example-project/global/networks/default"
    }
  }
}

mock_provider "time" {}

variables {
  project = "example-project"
  region  = "us-central1"
  zone    = "us-central1-a"
}

run "defaults" {
  command = plan

  assert {
    condition     = google_compute_instance.osworld.machine_type == "n2-standard-8"
    error_message = "the default machine is n2-standard-8"
  }
  assert {
    condition     = google_compute_instance.osworld.advanced_machine_features[0].enable_nested_virtualization
    error_message = "OSWorld's VM needs nested virtualization"
  }
  assert {
    condition     = google_compute_instance.osworld.scheduling[0].provisioning_model == "SPOT" && google_compute_instance.osworld.scheduling[0].instance_termination_action == "STOP"
    error_message = "the default is a Spot machine that stops when reclaimed"
  }
  assert {
    condition     = length(google_compute_instance.osworld.network_interface[0].access_config) == 0
    error_message = "with ssh = iap the machine has no external IP"
  }
  assert {
    condition     = google_compute_firewall.ssh.source_ranges == toset(["35.235.240.0/20"])
    error_message = "with ssh = iap only IAP's range reaches port 22"
  }
  assert {
    condition     = length(google_compute_router_nat.nat) == 1
    error_message = "with ssh = iap the machine gets Cloud NAT"
  }
  assert {
    condition     = google_compute_resource_policy.nightly_stop[0].instance_schedule_policy[0].vm_stop_schedule[0].schedule == "0 2 * * *"
    error_message = "the nightly stop is at 02:00"
  }
  assert {
    condition     = google_compute_instance.osworld.metadata["enable-oslogin"] == "TRUE" && google_compute_instance.osworld.metadata["idle-shutdown-minutes"] == "60"
    error_message = "OS Login is on and the idle shutdown is 60 minutes"
  }
  assert {
    condition     = google_compute_instance.osworld.labels == tomap({ app = "typesafe-computer-use", purpose = "osworld" }) && google_compute_instance.osworld.boot_disk[0].initialize_params[0].labels == tomap({ app = "typesafe-computer-use", purpose = "osworld" })
    error_message = "the machine and its disk carry the app and purpose labels"
  }
  assert {
    condition     = length(google_billing_budget.osworld) == 0
    error_message = "no billing account means no budget"
  }
  assert {
    condition     = output.ssh_command == "gcloud compute ssh osworld --project example-project --zone us-central1-a --tunnel-through-iap"
    error_message = "the ssh command goes through IAP"
  }
}

run "external_ip" {
  command = plan

  variables {
    ssh             = "external_ip"
    ssh_source_cidr = "203.0.113.7/32"
  }

  assert {
    condition     = length(google_compute_instance.osworld.network_interface[0].access_config) == 1
    error_message = "with ssh = external_ip the machine has an external IP"
  }
  assert {
    condition     = google_compute_firewall.ssh.source_ranges == toset(["203.0.113.7/32"])
    error_message = "with ssh = external_ip only ssh_source_cidr reaches port 22"
  }
  assert {
    condition     = length(google_compute_router_nat.nat) == 0
    error_message = "an external IP needs no NAT"
  }
}

run "external_ip_needs_a_source_range" {
  command = plan

  variables {
    ssh = "external_ip"
  }

  expect_failures = [var.ssh_source_cidr]
}

run "zone_outside_region" {
  command = plan

  variables {
    zone = "europe-west4-a"
  }

  expect_failures = [var.zone]
}

run "guards_off_and_budget_on" {
  command = plan

  variables {
    spot                  = false
    create_nat            = false
    nightly_stop_hour     = null
    idle_shutdown_minutes = 0
    billing_account       = "000000-000000-000000"
    budget_usd            = 25
  }

  assert {
    condition     = google_compute_instance.osworld.scheduling[0].provisioning_model == "STANDARD" && google_compute_instance.osworld.scheduling[0].automatic_restart
    error_message = "spot = false makes a standard machine"
  }
  assert {
    condition     = length(google_compute_router_nat.nat) == 0 && length(google_compute_resource_policy.nightly_stop) == 0 && length(google_project_iam_member.schedule_agent) == 0
    error_message = "create_nat = false and nightly_stop_hour = null make no NAT, schedule, or grant"
  }
  assert {
    condition     = length(google_compute_instance.osworld.resource_policies) == 0
    error_message = "no schedule is attached"
  }
  assert {
    condition     = google_billing_budget.osworld[0].amount[0].specified_amount[0].units == "25"
    error_message = "a billing account makes a budget of budget_usd"
  }
}
