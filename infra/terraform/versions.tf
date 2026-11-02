terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }

  # Remote state for real use. Left commented so `terraform init` works
  # without an existing bucket (CI runs `init -backend=false`).
  # backend "s3" {
  #   bucket       = "my-tf-state"
  #   key          = "pmi-platform/terraform.tfstate"
  #   region       = "us-west-2"
  #   use_lockfile = true
  #   encrypt      = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
