output "instance_id" {
  description = "EC2 instance id (use with `aws ssm start-session --target`)."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Elastic IP of the host."
  value       = aws_eip.app.public_ip
}

output "dashboard_url" {
  description = "Dashboard URL once the stack has finished bootstrapping."
  value       = "http://${aws_eip.app.public_ip}/"
}

output "alarm_topic_arn" {
  description = "SNS topic every alarm publishes to."
  value       = aws_sns_topic.alarms.arn
}

output "slack_webhook_parameter" {
  description = "SSM parameter to populate with the Slack webhook URL."
  value       = aws_ssm_parameter.slack_webhook.name
}
