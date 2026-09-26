# Whoever runs this supplies the project, region, and zone, in terraform.tfvars (git-ignored; see
# terraform.tfvars.example) or as TF_VAR_* environment variables. Everything else has a default.

variable "project" {
  description = "Google Cloud project ID to create the machine in."
  type        = string
}

variable "region" {
  description = "Region for the Cloud NAT and the nightly-stop schedule, e.g. us-central1."
  type        = string
}

variable "zone" {
  description = "Zone for the machine, inside region, e.g. us-central1-a."
  type        = string

  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "zone must be in region: us-central1-a is in us-central1."
  }
}

variable "instance_name" {
  description = "Name of the machine; the firewall rule, router, NAT, and schedule are named after it."
  type        = string
  default     = "osworld"

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,40}[a-z0-9])?$", var.instance_name))
    error_message = "instance_name must be lowercase letters, digits, and hyphens, start with a letter, and be at most 42 characters."
  }
}

variable "network" {
  description = "VPC network for the machine."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Subnetwork in region for the machine. null uses the one named like the network, which is what an auto-mode network such as default has."
  type        = string
  default     = null
}

variable "ssh" {
  description = "How SSH reaches the machine: iap (no external IP; through Identity-Aware Proxy) or external_ip (an external IP, open to ssh_source_cidr only)."
  type        = string
  default     = "iap"

  validation {
    condition     = contains(["iap", "external_ip"], var.ssh)
    error_message = "ssh must be iap or external_ip."
  }
}

variable "ssh_source_cidr" {
  description = "With ssh = external_ip, the only range allowed to reach port 22, e.g. your address as 203.0.113.7/32. Unused with iap."
  type        = string
  default     = ""

  validation {
    condition     = var.ssh != "external_ip" || can(cidrhost(var.ssh_source_cidr, 0))
    error_message = "ssh = external_ip needs ssh_source_cidr set to a CIDR range, e.g. 203.0.113.7/32."
  }
}

variable "create_nat" {
  description = "With ssh = iap, create a Cloud Router and Cloud NAT so the machine reaches the internet. Set false when the network already has NAT for the subnetwork."
  type        = bool
  default     = true
}

variable "machine_type" {
  description = "Machine type. It must support nested virtualization, which means an Intel one such as n2."
  type        = string
  default     = "n2-standard-8"
}

variable "spot" {
  description = "Run as a Spot VM: about half the price, and Google may stop it at any time, which only costs a rerun of the task."
  type        = bool
  default     = true
}

variable "disk_gb" {
  description = "Boot disk size in GB: Docker, OSWorld's VM image, and results."
  type        = number
  default     = 150

  validation {
    condition     = var.disk_gb >= 64
    error_message = "disk_gb must be at least 64: OSWorld's VM image alone takes tens of GB."
  }
}

variable "image" {
  description = "Boot image or image family. Changing it later does not rebuild an existing machine."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "idle_shutdown_minutes" {
  description = "Power the machine off after this many minutes with no OSWorld run and no SSH connection. 0 turns this guard off."
  type        = number
  default     = 60

  validation {
    condition     = var.idle_shutdown_minutes >= 0 && floor(var.idle_shutdown_minutes) == var.idle_shutdown_minutes
    error_message = "idle_shutdown_minutes must be a whole number, 0 or more."
  }
}

variable "nightly_stop_hour" {
  description = "Hour of the day (0 to 23, in time_zone) at which a schedule stops the machine. null turns this guard off."
  type        = number
  default     = 2

  validation {
    condition     = var.nightly_stop_hour == null ? true : (var.nightly_stop_hour >= 0 && var.nightly_stop_hour <= 23 && floor(var.nightly_stop_hour) == var.nightly_stop_hour)
    error_message = "nightly_stop_hour must be a whole hour from 0 to 23, or null."
  }
}

variable "time_zone" {
  description = "IANA time zone for nightly_stop_hour, e.g. America/New_York."
  type        = string
  default     = "Etc/UTC"
}

variable "grant_schedule_permission" {
  description = "Give the project's Compute Engine service agent the Compute Instance Admin (v1) role, which the nightly stop needs. This needs permission to change the project's IAM policy, and `down` removes the grant again; set false when an administrator has granted it already, or when something else in the project relies on it."
  type        = bool
  default     = true
}

variable "billing_account" {
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) for a budget with alerts at 50, 90, and 100 percent of budget_usd. Empty makes no budget; a budget needs billing permissions many users lack."
  type        = string
  default     = ""

  validation {
    condition     = var.billing_account == "" || can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account))
    error_message = "billing_account must be empty or an ID like 012345-6789AB-CDEF01."
  }
}

variable "budget_usd" {
  description = "Monthly budget in US dollars, used when billing_account is set."
  type        = number
  default     = 50

  validation {
    condition     = var.budget_usd > 0 && floor(var.budget_usd) == var.budget_usd
    error_message = "budget_usd must be a whole number of dollars above 0."
  }
}

variable "repo_url" {
  description = "Git URL the machine clones this project from."
  type        = string
  default     = "https://github.com/awlevin/typesafe-computer-use"
}

variable "repo_ref" {
  description = "Branch, tag, or commit the machine checks out before scripts/osworld-gcp first syncs a local copy over it."
  type        = string
  default     = "main"
}
