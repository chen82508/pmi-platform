# Alarm fan-out: CloudWatch → SNS → Lambda → Slack incoming webhook.

resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# The webhook URL is a secret, so Terraform only creates the parameter with a
# placeholder and never tracks its value (and so never writes it to state).
# Set it once with:
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /pmi-platform/slack-webhook-url --value https://hooks.slack.com/services/...
resource "aws_ssm_parameter" "slack_webhook" {
  name        = var.slack_webhook_parameter_name
  description = "Slack incoming-webhook URL for ${var.project} alarms"
  type        = "SecureString"
  value       = "UNSET"

  lifecycle {
    ignore_changes = [value]
  }
}

data "archive_file" "slack_notifier" {
  type        = "zip"
  source_file = "${path.module}/lambda/slack_notifier/handler.py"
  output_path = "${path.module}/.build/slack_notifier.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "slack_notifier" {
  name               = "${var.project}-slack-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "slack_notifier" {
  statement {
    sid       = "ReadWebhook"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.slack_webhook.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.slack_notifier.arn}:*"]
  }
}

resource "aws_iam_role_policy" "slack_notifier" {
  name   = "slack-notifier"
  role   = aws_iam_role.slack_notifier.id
  policy = data.aws_iam_policy_document.slack_notifier.json
}

resource "aws_cloudwatch_log_group" "slack_notifier" {
  name              = "/aws/lambda/${var.project}-slack-notifier"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "slack_notifier" {
  function_name    = "${var.project}-slack-notifier"
  role             = aws_iam_role.slack_notifier.arn
  runtime          = "python3.13"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.slack_notifier.output_path
  source_code_hash = data.archive_file.slack_notifier.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      WEBHOOK_PARAMETER = aws_ssm_parameter.slack_webhook.name
      ENVIRONMENT       = var.environment
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.slack_notifier.name
  }
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

resource "aws_sns_topic_subscription" "slack_notifier" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_notifier.arn
}
