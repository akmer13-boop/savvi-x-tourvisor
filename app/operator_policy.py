from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


OperatorStatus = Literal["active_contract", "approved_to_contract", "blocked"]
VALID_OPERATOR_STATUSES = {
    "active_contract",
    "approved_to_contract",
    "blocked",
}


class OperatorPolicyConfigurationError(RuntimeError):
    """The operator registry cannot be used safely."""


@dataclass(frozen=True)
class OperatorEntry:
    tourvisor_id: int
    name: str
    status: OperatorStatus
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorPolicy:
    version: str
    entries: tuple[OperatorEntry, ...]
    sha256: str
    enforced: bool

    @property
    def active_ids(self) -> frozenset[int]:
        return frozenset(
            entry.tourvisor_id
            for entry in self.entries
            if entry.status == "active_contract"
        )

    @property
    def active_count(self) -> int:
        return len(self.active_ids)

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


def _empty_policy(*, enforced: bool) -> OperatorPolicy:
    canonical = b'{"operators":[],"version":"unconfigured"}'
    return OperatorPolicy(
        version="unconfigured",
        entries=(),
        sha256=hashlib.sha256(canonical).hexdigest(),
        enforced=enforced,
    )


def load_operator_policy(path: str | Path, *, required: bool) -> OperatorPolicy:
    """Load and strictly validate the versioned server-side operator registry."""
    registry_path = Path(path)
    if not registry_path.is_file():
        if required:
            raise OperatorPolicyConfigurationError(
                f"Operator registry is missing: {registry_path}"
            )
        return _empty_policy(enforced=False)

    try:
        payload: Any = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorPolicyConfigurationError(
            f"Operator registry cannot be read: {registry_path}"
        ) from exc

    if not isinstance(payload, dict):
        raise OperatorPolicyConfigurationError("Operator registry root must be an object")

    version = str(payload.get("version") or "").strip()
    if not version:
        raise OperatorPolicyConfigurationError("Operator registry version is required")

    raw_operators = payload.get("operators")
    if not isinstance(raw_operators, list):
        raise OperatorPolicyConfigurationError("Operator registry operators must be a list")

    entries: list[OperatorEntry] = []
    seen_ids: set[int] = set()
    for index, raw in enumerate(raw_operators):
        if not isinstance(raw, dict):
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} must be an object"
            )

        raw_id = raw.get("tourvisor_id")
        if isinstance(raw_id, bool):
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} has an invalid tourvisor_id"
            )
        try:
            tourvisor_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} has an invalid tourvisor_id"
            ) from exc
        if tourvisor_id <= 0:
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} has a non-positive tourvisor_id"
            )
        if tourvisor_id in seen_ids:
            raise OperatorPolicyConfigurationError(
                f"Duplicate tourvisor_id in operator registry: {tourvisor_id}"
            )
        seen_ids.add(tourvisor_id)

        name = str(raw.get("name") or "").strip()
        if not name:
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} has no name"
            )

        status = str(raw.get("status") or "").strip()
        if status not in VALID_OPERATOR_STATUSES:
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} has an invalid status"
            )

        raw_aliases = raw.get("aliases") or []
        if not isinstance(raw_aliases, list):
            raise OperatorPolicyConfigurationError(
                f"Operator registry item {index} aliases must be a list"
            )
        aliases = tuple(
            alias
            for alias in (str(value).strip() for value in raw_aliases)
            if alias
        )

        entries.append(
            OperatorEntry(
                tourvisor_id=tourvisor_id,
                name=name,
                status=status,  # type: ignore[arg-type]
                aliases=aliases,
            )
        )

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    policy = OperatorPolicy(
        version=version,
        entries=tuple(entries),
        sha256=hashlib.sha256(canonical).hexdigest(),
        enforced=required,
    )
    if required and not policy.active_ids:
        raise OperatorPolicyConfigurationError(
            "Operator registry must contain at least one active_contract operator"
        )
    return policy
