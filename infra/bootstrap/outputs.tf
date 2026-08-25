output "state_bucket_name" {
  description = "Generated private S3 bucket name for explicit application backend initialization."
  value       = aws_s3_bucket.state.bucket
}

output "backend_region" {
  description = "Region to pass explicitly when initializing the application S3 backend."
  value       = var.region
}
