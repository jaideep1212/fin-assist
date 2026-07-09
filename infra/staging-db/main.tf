# Ephemeral staging Postgres (Burstable B1ms). Created by Job B at the start of
# a cycle and destroyed at the end -- so it holds only that day's encrypted data
# and costs nothing between runs.

resource "azurerm_postgresql_flexible_server" "staging" {
  name                          = var.server_name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  version                       = "16"
  administrator_login           = var.admin_login
  administrator_password        = var.admin_password
  sku_name                      = "B_Standard_B1ms"   # Burstable, cheapest tier
  storage_mb                    = 32768               # 32 GB (minimum)
  auto_grow_enabled             = false
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false               # transient; no geo backup
  public_network_access_enabled = true                # reached via firewall rule
  zone                          = "1"
}

# "Allow Azure services" convention: the 0.0.0.0/0.0.0.0 rule lets resources
# inside Azure (your Container Apps job) reach the server without a public IP
# allowlist. Fine for transient, encrypted-only data.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.staging.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Empty database; the loader creates the staging schema + tables inside it.
resource "azurerm_postgresql_flexible_server_database" "staging" {
  name      = var.database_name
  server_id = azurerm_postgresql_flexible_server.staging.id
  collation = "en_US.utf8"
  charset   = "utf8"
}
