variable "resource_group_name" {
  type    = string
  default = "fin-assist-rg"
}

variable "location" {
  type    = string
  default = "northeurope"
}

variable "server_name" {
  type    = string
  default = "fin-assist-staging-db" # globally unique
}

variable "database_name" {
  type    = string
  default = "staging"
}

variable "admin_login" {
  type    = string
  default = "stagingadmin"
}

variable "admin_password" {
  type      = string
  sensitive = true
  # supplied via env var: export TF_VAR_admin_password
}