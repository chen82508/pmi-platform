# infra/terraform

Single-host AWS deployment for the PMI platform, with CloudWatch alarms that
post to Slack.

```
                    ┌──────────────── VPC 10.20.0.0/16 ────────────────┐
  users ── :80 ───▶ │  EC2 (Ubuntu 24.04, docker compose, CW agent)     │
                    │    IMDSv2 · encrypted gp3 · Elastic IP · SSM      │
                    └───────────────┬──────────────────────────────────┘
                                    │ metrics: AWS/EC2 + agent (mem, disk)
                                    ▼
              CloudWatch alarms ── cpu · memory · disk · system/instance checks
                                    │                   └─ ec2:recover / ec2:reboot
                                    ▼
                               SNS topic ──▶ Lambda (slack_notifier) ──▶ Slack
                                    └──────▶ e-mail (optional)
```

## Resources

| File | What it creates |
|---|---|
| `network.tf` | VPC, internet gateway, public subnet, route table, and a security group that only allows HTTP 80 in |
| `iam.tf` | Instance role with `AmazonSSMManagedInstanceCore` (Session Manager instead of SSH) and `CloudWatchAgentServerPolicy` |
| `ec2.tf` | Ubuntu 24.04 instance (AMI resolved from Canonical's SSM parameter), IMDSv2 required, encrypted gp3 root volume, Elastic IP |
| `templates/user_data.sh.tftpl` | First boot: Docker Engine, CloudWatch agent, repository clone, the compose stack as a systemd unit, migrations, and the fixture demo |
| `monitoring.tf` | Five alarms. CPU and the status checks use `AWS/EC2` metrics; memory and disk use metrics from the CloudWatch agent |
| `notifications.tf` | SNS topic, optional e-mail subscription, the SSM SecureString for the webhook, and the Slack notifier Lambda with its least-privilege role and log group |
| `lambda/slack_notifier/` | Standard-library Python handler that turns a CloudWatch alarm into a Slack Block Kit message, plus its tests |

### Alarm design

| Alarm | Condition | Action |
|---|---|---|
| `cpu-high` | average CPU above 80% for 15 minutes | Slack |
| `memory-high` | memory used above 90% for 10 minutes | Slack |
| `disk-high` | root filesystem above 85% used | Slack |
| `system-check-failed` | hardware or hypervisor fault for 2 minutes | `ec2:recover` and Slack |
| `instance-check-failed` | operating system fault for 3 minutes | `ec2:reboot` and Slack |

Every alarm also notifies when it returns to OK. The memory and disk alarms
treat missing data as breaching, so a dead CloudWatch agent (or a dead host)
raises an alert instead of going quiet. The agent publishes an aggregate with
the `InstanceId` dimension only, so the alarm dimensions match exactly no
matter which device name the root volume gets.

### Why the webhook is not a Terraform variable

Anything Terraform manages ends up in its state file. The webhook URL is a
credential, so Terraform only creates the SSM parameter with a placeholder
value and ignores changes to it. The real URL is written once, out of band.
Until that happens, the Lambda logs an error and drops messages instead of
failing.

## Usage

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # region, CIDRs, alarm e-mail
terraform init
terraform plan
terraform apply

# Store the Slack incoming-webhook URL; Terraform never reads this value.
aws ssm put-parameter --overwrite --type SecureString \
  --name "$(terraform output -raw slack_webhook_parameter)" \
  --value 'https://hooks.slack.com/services/...'

# Open a shell on the host without SSH.
aws ssm start-session --target "$(terraform output -raw instance_id)"
```

State uses the local backend by default. For shared use, uncomment the `s3`
backend block in `versions.tf`.

`repo_url` must be cloneable without credentials. For a private repository,
bake an AMI or fetch a deploy key from SSM in `user_data` instead.

## Validation

CI runs `terraform fmt -check`, `terraform validate` and `tflint` with the
AWS ruleset, plus the Lambda's tests. None of these need AWS credentials. To
run the same checks locally:

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
tflint --init && tflint
(cd lambda/slack_notifier && pytest -q)
```

## Scope and next steps

This is the smallest setup that is still production-shaped: one host, with
Postgres on the instance's own volume. For a larger deployment, the natural
next steps are:

- Move Postgres to RDS, with pgvector support and automated backups.
- Put the dashboard behind an ALB with an ACM certificate for HTTPS.
- Build images in CI, push them to ECR, and deploy with tags pinned to a
  commit SHA.
