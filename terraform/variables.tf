variable "aws_region" {
  description = "AWS region (must be us-east-1 when deploying CloudFront WAFv2 WebACL)"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix applied to all provisioned serverless resources"
  type        = string
  default     = "serverless-crm"
}

variable "environment" {
  description = "Deployment environment stage name"
  type        = string
  default     = "prod"
}

variable "waf_rate_limit" {
  description = "Maximum requests per 5-minute sliding window from a single IP before WAF blocks"
  type        = number
  default     = 1000
}

variable "blocked_countries" {
  description = "ISO 3166-1 alpha-2 country codes blocked at the CloudFront WAF edge"
  type        = list(string)
  default     = ["KP", "SY"]
}

variable "enable_api_cache" {
  description = "Enable API Gateway dedicated cache cluster on the stage"
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Optional email address subscribed to CloudWatch and Enterprise customer SNS alerts"
  type        = string
  default     = ""
}
