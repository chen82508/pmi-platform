---
name: pmi-infra-terraform
description: >-
  Working on infra/terraform: the single EC2 host, its user_data bootstrap,
  CloudWatch alarms, and the SNS → Lambda → Slack alert path. Covers validating
  without AWS credentials (fmt, validate, tflint, rendering templates offline),
  adding an alarm correctly, changing the Slack notifier, provider lock files,
  and deploying or tearing down. Use for any change under infra/, any alarm or
  notification question, or a request to deploy the stack to AWS.
---

# Infrastructure (Terraform)

```
infra/terraform/
  versions.tf         providers (aws ~> 6, archive), commented S3 backend
  variables.tf        every knob, with defaults and validation
  network.tf          VPC, public subnet, IGW, SG (HTTP 80 in, all out; no SSH)
  iam.tf              instance role: SSM Session Manager + CloudWatch agent
  ec2.tf              Ubuntu 24.04 (Canonical SSM parameter), IMDSv2, encrypted gp3, EIP
  monitoring.tf       the five alarms
  notifications.tf    SNS, optional e-mail, SSM webhook parameter, Lambda + role + logs
  templates/          user_data.sh.tftpl, cloudwatch-agent.json.tftpl
  lambda/slack_notifier/   handler.py (stdlib only) + tests
```

Nothing has to be applied to work here: CI validates everything without AWS
credentials.

## 1. Validate (what CI runs)

```bash
just tf-check                              # fmt -check, init -backend=false, validate
cd infra/terraform && tflint --init && tflint     # AWS ruleset, see .tflint.hcl
cd lambda/slack_notifier && uvx ruff check . && uvx --with pytest pytest -q
```

Render the templates offline before changing them. `terraform console` needs
no credentials but reads one expression per line:

```bash
AGENT='templatefile("templates/cloudwatch-agent.json.tftpl", {namespace="pmi-platform/host"})'
echo "templatefile(\"templates/user_data.sh.tftpl\", {repo_url=\"https://example.com/r.git\", repo_ref=\"main\", environment=\"dev\", cloudwatch_agent_config=$AGENT})" \
  | terraform console
```

Then check the shell with `bash -n` and the agent config with a JSON parser.
In templates, `$${...}` is a literal `${...}` (the agent's own
`${aws:InstanceId}`); `%{ if }` blocks are template logic.

## 2. Adding or changing an alarm

- Point `alarm_actions` **and** `ok_actions` at `local.alarm_actions`, so Slack
  hears both the alert and the recovery.
- **EC2 metrics** (`AWS/EC2`) use `local.instance_dims`.
- **Agent metrics** (`local.agent_namespace`): the agent publishes an
  aggregate with only the `InstanceId` dimension (`aggregation_dimensions`).
  Alarm on that exact dimension set; an alarm whose dimensions don't match a
  published series never fires. A new agent metric needs adding to
  `cloudwatch-agent.json.tftpl` first.
- `treat_missing_data = "breaching"` for anything a dead agent or host should
  page on; `"missing"` for metrics that may legitimately pause.
- Recovery actions: `arn:aws:automate:<region>:ec2:recover` (system check) and
  `...:ec2:reboot` (instance check).
- Name alarms `${var.project}-<what>` and describe the condition in
  `alarm_description`; the Slack message shows both.

## 3. The Slack notifier

- `handler.py` parses the alarm JSON from each SNS record (plain-text messages
  are wrapped), builds a Block Kit message, and posts to the URL in the SSM
  parameter. Standard library only, so `archive_file` zips one file; adding a
  dependency means a packaging step.
- The webhook URL is a secret: Terraform creates the SSM SecureString with
  `UNSET` and `ignore_changes = [value]`, so it never enters state. While it is
  unset, the Lambda logs an error and drops the message instead of failing.
- Keep the IAM policy least-privilege: `ssm:GetParameter` on that one
  parameter, and log writes on its own log group.
- Every change needs a test in `test_handler.py` (the post is monkeypatched;
  tests never call Slack).

## 4. Providers

`.terraform.lock.hcl` is committed. After changing provider constraints:

```bash
terraform init -upgrade -backend=false
terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64
```

Without the Linux hashes, CI's `terraform init` fails verification.

## 5. Deploy and tear down

Applying creates billable resources (a `t3.large` by default, EBS, EIP, Lambda,
CloudWatch). Confirm with the user before `apply` or `destroy`.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # region, CIDRs, alarm e-mail
terraform init && terraform plan && terraform apply
aws ssm put-parameter --overwrite --type SecureString \
  --name "$(terraform output -raw slack_webhook_parameter)" --value '<webhook URL>'
aws ssm start-session --target "$(terraform output -raw instance_id)"   # shell, no SSH
terraform destroy                               # when done
```

- First boot installs Docker and the agent, clones `repo_url`, starts the stack
  as the `pmi` systemd unit, and runs the fixture demo; give it ~10 minutes,
  then open `terraform output dashboard_url`. Follow progress in
  `/var/log/cloud-init-output.log`.
- `repo_url` must be cloneable without credentials. A private repository needs
  a baked AMI or a deploy key fetched from SSM in `user_data`.
- Changing `user_data` replaces the instance (`user_data_replace_on_change`);
  the AMI is in `ignore_changes`, so Ubuntu point releases don't.
- To test an alarm end to end:
  `aws cloudwatch set-alarm-state --alarm-name <name> --state-value ALARM --state-reason test`.
