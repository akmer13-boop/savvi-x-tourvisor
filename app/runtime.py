import logging

from app.config import settings
from app.operator_policy import load_operator_policy
from app.search_guard import SearchGuard


logger = logging.getLogger(__name__)

operator_policy = load_operator_policy(
    settings.operator_registry_path,
    required=settings.operator_policy_required,
)
settings.validate_runtime_configuration(operator_policy.active_count)

logger.info(
    "TOURVISOR_FLIGHT_RUNTIME_CONFIG enabled=%s limit=%s concurrency=%s "
    "effective_concurrency=%s timeout_seconds=%s",
    settings.tourvisor_enable_flight_actualization,
    settings.tourvisor_flight_actualization_limit,
    settings.tourvisor_flight_actualization_concurrency,
    settings.effective_flight_actualization_concurrency,
    settings.tourvisor_flight_timeout_seconds,
)

search_guard: SearchGuard | None = None
if settings.search_guard_enabled:
    search_guard = SearchGuard(
        settings.search_guard_db_path,
        settings.search_guard_hmac_secret,
        namespace=settings.search_guard_namespace,
        window_ttl_seconds=settings.search_guard_ttl_seconds,
        replay_ttl_seconds=settings.search_result_replay_ttl_seconds,
        max_dispatches=settings.search_guard_max_searches,
    )
    search_guard.check_ready()
    search_guard.prune_expired()
