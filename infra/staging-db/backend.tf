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
    # --- Static settings: identical for every stage --------------------------
    # This file is copied verbatim into each stage folder (infra/<stage>).
    # Do NOT hardcode the state `key` here — see the note at the bottom.
    resource_group_name  = "fin-assist-rg"
    storage_account_name = "finassisttfstate"   # the remote-state storage account
    container_name       = "tfstate"                # one shared container holds every stage's state

    # Authenticate to the storage account with an Entra ID (Azure AD) token
    # instead of a storage access key. Locally that token comes from your
    # `az login`; inside the orchestrator container it comes from the job's
    # managed identity (ARM_USE_MSI / ARM_CLIENT_ID env vars on the job). This
    # is why there's no `az login` in the container and no key in this file.
    use_azuread_auth = true

    # --- Per-stage setting: intentionally OMITTED ----------------------------
    # `key` (the state blob filename) is deliberately NOT set here. It's supplied
    # at init time by run_staging_pipeline.py:
    #
    #     terraform -chdir=infra/<stage> init -reconfigure \
    #         -backend-config="key=<stage>.tfstate"
    #
    # This is Terraform "partial backend configuration". Leaving `key` out is
    # what lets this one file serve every stage unchanged — staging-db writes to
    # staging-db.tfstate, staging-app to staging-app.tfstate — same code,
    # separate isolated state.
  }
}