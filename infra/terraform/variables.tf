variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-west-2"
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "pmi-platform"
}

variable "environment" {
  description = "Deployment environment tag (e.g. dev, prod)."
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR block for the single public subnet."
  type        = string
  default     = "10.20.1.0/24"
}

variable "instance_type" {
  description = "EC2 instance type for the docker compose host (x86_64)."
  type        = string
  default     = "t3.large"
}

variable "root_volume_gb" {
  description = "Size of the encrypted gp3 root volume. Postgres lives here, so size for the time-series tables."
  type        = number
  default     = 100

  validation {
    condition     = var.root_volume_gb >= 30
    error_message = "The root volume must be at least 30 GB (images + Postgres)."
  }
}

variable "http_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the dashboard on port 80."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "repo_url" {
  description = "Git URL the instance clones on first boot. Leave empty to skip the clone (e.g. when baking an AMI)."
  type        = string
  default     = "https://github.com/chen82508/pmi-platform.git"
}

variable "repo_ref" {
  description = "Branch or tag to check out."
  type        = string
  default     = "main"
}

# ── alarms ───────────────────────────────────────────────────────────────────

variable "cpu_alarm_threshold" {
  description = "Average CPU utilisation (%) that triggers the CPU alarm."
  type        = number
  default     = 80
}

variable "memory_alarm_threshold" {
  description = "Memory used (%) that triggers the memory alarm."
  type        = number
  default     = 90
}

variable "disk_alarm_threshold" {
  description = "Root filesystem used (%) that triggers the disk alarm."
  type        = number
  default     = 85
}

variable "alarm_email" {
  description = "Optional e-mail address subscribed to the alarm topic (needs confirming). Empty = Slack only."
  type        = string
  default     = ""
}

variable "slack_webhook_parameter_name" {
  description = "SSM SecureString holding the Slack incoming-webhook URL. Terraform creates it with a placeholder; set the real value out of band."
  type        = string
  default     = "/pmi-platform/slack-webhook-url"
}

variable "log_retention_days" {
  description = "Retention for the notifier Lambda's log group."
  type        = number
  default     = 30
}
