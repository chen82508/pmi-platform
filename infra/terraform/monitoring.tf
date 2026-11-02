# CloudWatch alarms for the compose host. Every alarm notifies the same SNS
# topic, which fans out to the Slack notifier (and optionally e-mail).

locals {
  agent_namespace = "${var.project}/host"
  alarm_actions   = [aws_sns_topic.alarms.arn]
  instance_dims   = { InstanceId = aws_instance.app.id }
}

resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "${var.project}-cpu-high"
  alarm_description   = "Average CPU above ${var.cpu_alarm_threshold}% for 15 minutes."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = local.instance_dims
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = var.cpu_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Hardware / hypervisor failure: let EC2 move the instance to healthy hardware
# (same instance id, EIP and EBS) and tell the team it happened.
resource "aws_cloudwatch_metric_alarm" "system_check_failed" {
  alarm_name          = "${var.project}-system-check-failed"
  alarm_description   = "EC2 system status check failing; automatic recovery triggered."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  dimensions          = local.instance_dims
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = concat(local.alarm_actions, ["arn:aws:automate:${var.aws_region}:ec2:recover"])
  ok_actions          = local.alarm_actions
}

# OS-level failure (kernel panic, exhausted memory, broken network config).
resource "aws_cloudwatch_metric_alarm" "instance_check_failed" {
  alarm_name          = "${var.project}-instance-check-failed"
  alarm_description   = "EC2 instance status check failing for 3 minutes; rebooting."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  dimensions          = local.instance_dims
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = concat(local.alarm_actions, ["arn:aws:automate:${var.aws_region}:ec2:reboot"])
  ok_actions          = local.alarm_actions
}

# The two agent metrics below treat missing data as breaching: if the agent
# (or the whole host) stops reporting, that is itself worth a page.
resource "aws_cloudwatch_metric_alarm" "memory_high" {
  alarm_name          = "${var.project}-memory-high"
  alarm_description   = "Memory used above ${var.memory_alarm_threshold}% for 10 minutes (or the CloudWatch agent stopped reporting)."
  namespace           = local.agent_namespace
  metric_name         = "mem_used_percent"
  dimensions          = local.instance_dims
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.memory_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Postgres time-series tables grow without bound; a full root volume stops the
# database, so warn well before it happens.
resource "aws_cloudwatch_metric_alarm" "disk_high" {
  alarm_name          = "${var.project}-disk-high"
  alarm_description   = "Root filesystem above ${var.disk_alarm_threshold}% used (or the CloudWatch agent stopped reporting)."
  namespace           = local.agent_namespace
  metric_name         = "disk_used_percent"
  dimensions          = local.instance_dims
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.disk_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
