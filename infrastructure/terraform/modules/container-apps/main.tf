# NOTE: HTTP ingress timeout is set to 960s via az containerapp ingress update
# in the GitHub Actions deploy workflow (not exposed by the azurerm Terraform provider).
# This must exceed max(pending_ttl_seconds) + 60s across all step-up policies.
# See .github/workflows/deploy-coordinator.yml for the post-deploy step.
