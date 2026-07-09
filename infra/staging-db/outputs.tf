output "server_fqdn" {
  value = azurerm_postgresql_flexible_server.staging.fqdn
}

# Feed this to the loader as STAGING_DATABASE_URL. sslmode=require is REQUIRED --
# Flexible Server rejects non-SSL connections by default.
output "staging_database_url" {
  value     = "postgresql://${var.admin_login}:${var.admin_password}@${azurerm_postgresql_flexible_server.staging.fqdn}:5432/${var.database_name}?sslmode=require"
  sensitive = true
}
