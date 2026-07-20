from app.config import settings
from app.operator_policy import load_operator_policy


operator_policy = load_operator_policy(
    settings.operator_registry_path,
    required=settings.operator_policy_required,
)
settings.validate_runtime_configuration(operator_policy.active_count)
