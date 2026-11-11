output "cloudfront_url" {
  description = "Unified CloudFront distribution URL fronting both Static SPA and REST API"
  value       = "https://${aws_cloudfront_distribution.cdn.domain_name}"
}

output "api_gateway_invoke_url" {
  description = "Direct regional API Gateway stage invoke URL"
  value       = aws_api_gateway_stage.prod.invoke_url
}

output "cognito_user_pool_id" {
  description = "Amazon Cognito User Pool ID"
  value       = aws_cognito_user_pool.crm_users.id
}

output "cognito_client_id" {
  description = "Amazon Cognito Web App Client ID"
  value       = aws_cognito_user_pool_client.web_client.id
}

output "cognito_hosted_ui_url" {
  description = "Amazon Cognito Hosted UI URL for OAuth2 Sign-In"
  value       = "https://${aws_cognito_user_pool_domain.hosted_ui.domain}.auth.${var.aws_region}.amazoncognito.com/login?client_id=${aws_cognito_user_pool_client.web_client.id}&response_type=token&scope=email+openid+profile&redirect_uri=https://${aws_cloudfront_distribution.cdn.domain_name}/"
}

output "dynamodb_customers_table" {
  description = "DynamoDB Customers table name"
  value       = aws_dynamodb_table.customers.name
}

output "dynamodb_audit_table" {
  description = "DynamoDB Stream Audit Logs table name"
  value       = aws_dynamodb_table.audit_logs.name
}

output "s3_frontend_bucket" {
  description = "Private S3 bucket hosting frontend static assets via CloudFront OAC"
  value       = aws_s3_bucket.frontend.id
}

output "waf_web_acl_arn" {
  description = "CloudFront WAFv2 Web ACL ARN"
  value       = aws_wafv2_web_acl.edge_waf.arn
}
