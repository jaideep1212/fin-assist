# Remote state so the daily apply and destroy read the SAME source of truth.
# Without this, a lost/mismatched local state can orphan the server (it keeps
# costing money) or break the destroy. Create the state storage ONCE, by hand,
# before the first init (it can't bootstrap itself):
#
#   az storage account create -g fin-assist-rg -n <stateacct> -l northeurope --sku Standard_LRS
#   az storage container create --account-name <stateacct> -n tfstate
#
terraform {
  backend "azurerm" {
    resource_group_name  = "fin-assist-rg"
    storage_account_name = "finassisttfstate"   # <-- the account you created above
    container_name       = "tfstate"
    key                  = "staging-db.tfstate"
    use_azuread_auth     = true
  }
}
