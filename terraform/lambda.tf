# AWS Lambda Functions, Event Source Mapping & X-Ray Tracing

data "archive_file" "lambdas_bundle" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas"
  output_path = "${path.module}/dist/lambdas_bundle.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "aws_sqs_queue" "stream_dlq" {
  name                      = "${var.project_name}-stream-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Name = "${var.project_name}-stream-dlq"
  }
}

# 1. Customers CRUD REST API Lambda
resource "aws_cloudwatch_log_group" "customers_api_logs" {
  name              = "/aws/lambda/${var.project_name}-customers-api-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "customers_api" {
  function_name    = "${var.project_name}-customers-api-${var.environment}"
  description      = "Handles authenticated CRUD operations on DynamoDB Customers and Audit tables"
  role             = aws_iam_role.customers_api_role.arn
  runtime          = "python3.12"
  handler          = "customers_api.handler.lambda_handler"
  filename         = data.archive_file.lambdas_bundle.output_path
  source_code_hash = data.archive_file.lambdas_bundle.output_base64sha256
  memory_size      = 256
  timeout          = 15

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT     = var.environment
      CUSTOMERS_TABLE = aws_dynamodb_table.customers.name
      AUDIT_TABLE     = aws_dynamodb_table.audit_logs.name
      CORS_ORIGIN     = "*"
    }
  }

  depends_on = [aws_cloudwatch_log_group.customers_api_logs]
}

# 2. DynamoDB Stream Processor Lambda
resource "aws_cloudwatch_log_group" "stream_processor_logs" {
  name              = "/aws/lambda/${var.project_name}-stream-processor-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "stream_processor" {
  function_name    = "${var.project_name}-stream-processor-${var.environment}"
  description      = "Consumes DynamoDB Streams (NEW_AND_OLD_IMAGES) to log audit history and notify SNS"
  role             = aws_iam_role.stream_processor_role.arn
  runtime          = "python3.12"
  handler          = "stream_processor.handler.lambda_handler"
  filename         = data.archive_file.lambdas_bundle.output_path
  source_code_hash = data.archive_file.lambdas_bundle.output_base64sha256
  memory_size      = 256
  timeout          = 30

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT      = var.environment
      AUDIT_TABLE      = aws_dynamodb_table.audit_logs.name
      ALERTS_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.stream_processor_logs]
}

resource "aws_lambda_event_source_mapping" "customers_stream" {
  event_source_arn                   = aws_dynamodb_table.customers.stream_arn
  function_name                      = aws_lambda_function.stream_processor.arn
  starting_position                  = "LATEST"
  batch_size                         = 10
  maximum_batching_window_in_seconds = 5
  maximum_retry_attempts             = 3
  bisect_batch_on_function_error     = true

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.stream_dlq.arn
    }
  }
}

# 3. Cognito Post-Confirmation Trigger Lambda
resource "aws_cloudwatch_log_group" "post_confirmation_logs" {
  name              = "/aws/lambda/${var.project_name}-post-confirm-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "post_confirmation" {
  function_name    = "${var.project_name}-post-confirm-${var.environment}"
  description      = "Cognito Post-Confirmation trigger recording onboarding audit trail"
  role             = aws_iam_role.cognito_trigger_role.arn
  runtime          = "python3.12"
  handler          = "cognito_triggers.post_confirmation.lambda_handler"
  filename         = data.archive_file.lambdas_bundle.output_path
  source_code_hash = data.archive_file.lambdas_bundle.output_base64sha256
  memory_size      = 128
  timeout          = 10

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ENVIRONMENT = var.environment
      AUDIT_TABLE = aws_dynamodb_table.audit_logs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.post_confirmation_logs]
}
