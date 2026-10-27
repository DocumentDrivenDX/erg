"""Pure, typed decisions shared by quality gates and their negative controls."""

from __future__ import annotations

from collections.abc import Mapping


def increased_counts(observed: Mapping[str, int], limits: Mapping[str, int]) -> list[str]:
    """Missing entries have a zero allowance; removed debt is always acceptable."""
    return [key for key, value in observed.items() if value > limits.get(key, 0)]


def weakened_floors(previous: Mapping[str, float], current: Mapping[str, float]) -> list[str]:
    return [key for key, value in previous.items() if current.get(key, -1) < value]


def permitted_skip(test_id: str, reason: str, platform: str, missing: set[str]) -> bool:
    native = {
        "test_file_events.FileEvents.test_real_file_events_drive_capture_without_inventory_walk": "FSEvents requires macOS",
        "test_host_counters.HostCounters.test_native_cpu_counters_advance_without_a_subprocess": "Mach counters require macOS",
    }
    jwt_cases = {
        "test_delegated_staff_token_and_role_type_confusion_rejected",
        "test_revocation_wrong_principal_and_ambiguous_registry_rejected",
        "test_same_generation_cannot_bind_two_installations_or_principals",
        "test_signed_application_identity_binds_active_device",
        "test_wrong_signature_issuer_audience_expiry_and_tenant_rejected",
    }
    if platform != "darwin" and native.get(test_id) == reason:
        return True
    if test_id.startswith("test_delivery_auth.Auth.") and test_id.rsplit(".", 1)[-1] in jwt_cases:
        return (
            bool({"jwt", "cryptography"} & missing)
            and reason == "central-only PyJWT/cryptography unavailable"
        )
    if (
        test_id
        == "test_receiver_index.ReceiverIndex.test_registered_http_trigger_names_match_handler_parameters"
    ):
        return (
            "azure.functions" in missing
            and reason == "central-only Azure Functions SDK unavailable"
        )
    return False
