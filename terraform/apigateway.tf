# Amazon API Gateway REST API, Cognito JWT Authorizer, Response Cache & Usage Plan

resource "aws_api_gateway_rest_api" "crm_api" {
  name        = "${var.project_name}-rest-api-${var.environment}"
  description = "Serverless Customer Records REST API secured by Amazon Cognito and AWS WAF"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_authorizer" "cognito" {
  name          = "${var.project_name}-cognito-authorizer"
  type          = "COGNITO_USER_POOLS"
  rest_api_id   = aws_api_gateway_rest_api.crm_api.id
  provider_arns = [aws_cognito_user_pool.crm_users.arn]
}

# ------------------------------------------------------------------------------
# REST Resources: /api, /api/health, /api/customers, /api/customers/{id}, /api/audit
# ------------------------------------------------------------------------------
resource "aws_api_gateway_resource" "api" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  parent_id   = aws_api_gateway_rest_api.crm_api.root_resource_id
  path_part   = "api"
}

resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  parent_id   = aws_api_gateway_resource.api.id
  path_part   = "health"
}

resource "aws_api_gateway_resource" "customers" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  parent_id   = aws_api_gateway_resource.api.id
  path_part   = "customers"
}

resource "aws_api_gateway_resource" "customer_id" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  parent_id   = aws_api_gateway_resource.customers.id
  path_part   = "{id}"
}

resource "aws_api_gateway_resource" "audit" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  parent_id   = aws_api_gateway_resource.api.id
  path_part   = "audit"
}

# ------------------------------------------------------------------------------
# Methods & Lambda Proxy Integrations (Uniform Map)
# ------------------------------------------------------------------------------
locals {
  api_routes = {
    health_get = {
      resource_id = aws_api_gateway_resource.health.id
      http_method = "GET"
      auth_type   = "NONE"
      use_auth    = false
      has_id      = false
    }
    customers_get = {
      resource_id = aws_api_gateway_resource.customers.id
      http_method = "GET"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = false
    }
    customers_post = {
      resource_id = aws_api_gateway_resource.customers.id
      http_method = "POST"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = false
    }
    customer_item_get = {
      resource_id = aws_api_gateway_resource.customer_id.id
      http_method = "GET"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = true
    }
    customer_item_put = {
      resource_id = aws_api_gateway_resource.customer_id.id
      http_method = "PUT"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = true
    }
    customer_item_delete = {
      resource_id = aws_api_gateway_resource.customer_id.id
      http_method = "DELETE"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = true
    }
    audit_get = {
      resource_id = aws_api_gateway_resource.audit.id
      http_method = "GET"
      auth_type   = "COGNITO_USER_POOLS"
      use_auth    = true
      has_id      = false
    }
  }

  cors_resources = {
    health      = aws_api_gateway_resource.health.id
    customers   = aws_api_gateway_resource.customers.id
    customer_id = aws_api_gateway_resource.customer_id.id
    audit       = aws_api_gateway_resource.audit.id
  }
}

resource "aws_api_gateway_method" "routes" {
  for_each = local.api_routes

  rest_api_id   = aws_api_gateway_rest_api.crm_api.id
  resource_id   = each.value.resource_id
  http_method   = each.value.http_method
  authorization = each.value.auth_type
  authorizer_id = each.value.use_auth ? aws_api_gateway_authorizer.cognito.id : null

  request_parameters = each.value.has_id ? {
    "method.request.path.id" = true
  } : {}
}

resource "aws_api_gateway_integration" "routes" {
  for_each = local.api_routes

  rest_api_id             = aws_api_gateway_rest_api.crm_api.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.routes[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.customers_api.invoke_arn
  cache_key_parameters    = each.value.has_id ? ["method.request.path.id"] : []
}

# ------------------------------------------------------------------------------
# Uniform CORS Preflight (OPTIONS) Integrations
# ------------------------------------------------------------------------------
resource "aws_api_gateway_method" "cors_options" {
  for_each = local.cors_resources

  rest_api_id   = aws_api_gateway_rest_api.crm_api.id
  resource_id   = each.value
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "cors_mock" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "cors_200" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "cors_200_resp" {
  for_each = local.cors_resources

  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  status_code = aws_api_gateway_method_response.cors_200[each.key].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amzn-Trace-Id'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }

  depends_on = [aws_api_gateway_integration.cors_mock]
}

# ------------------------------------------------------------------------------
# Stage, Response Caching, X-Ray Tracing, and Usage Plan
# ------------------------------------------------------------------------------
resource "aws_api_gateway_deployment" "prod" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      local.api_routes,
      aws_api_gateway_method.routes,
      aws_api_gateway_integration.routes,
      aws_api_gateway_method.cors_options,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.routes,
    aws_api_gateway_integration_response.cors_200_resp
  ]
}

resource "aws_cloudwatch_log_group" "apigw_access_logs" {
  name              = "/aws/apigateway/${var.project_name}-${var.environment}"
  retention_in_days = 14
}

resource "aws_api_gateway_stage" "prod" {
  deployment_id         = aws_api_gateway_deployment.prod.id
  rest_api_id           = aws_api_gateway_rest_api.crm_api.id
  stage_name            = var.environment
  xray_tracing_enabled  = true
  cache_cluster_enabled = var.enable_api_cache
  cache_cluster_size    = var.enable_api_cache ? "0.5" : null

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access_logs.arn
    format = jsonencode({
      requestId    = "$context.requestId"
      ip           = "$context.identity.sourceIp"
      httpMethod   = "$context.httpMethod"
      resourcePath = "$context.resourcePath"
      status       = "$context.status"
      latencyMs    = "$context.responseLatency"
      xrayTraceId  = "$context.xrayTraceId"
    })
  }

  depends_on = [aws_api_gateway_account.main]
}

resource "aws_api_gateway_method_settings" "all_methods" {
  rest_api_id = aws_api_gateway_rest_api.crm_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = true
    logging_level          = "INFO"
    throttling_burst_limit = 50
    throttling_rate_limit  = 100
    caching_enabled        = var.enable_api_cache
    cache_ttl_in_seconds   = 60
  }
}

resource "aws_api_gateway_usage_plan" "standard" {
  name = "${var.project_name}-usage-plan-${var.environment}"

  api_stages {
    api_id = aws_api_gateway_rest_api.crm_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  quota_settings {
    limit  = 50000
    period = "MONTH"
  }

  throttle_settings {
    burst_limit = 50
    rate_limit  = 100
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.customers_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.crm_api.execution_arn}/*/*"
}
