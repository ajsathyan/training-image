"""Build and submit a bounded, evidence-backed Fleet read-model bootstrap."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "agora.fleet-read-model-bootstrap.v1"
SUPPORTED_SCOPES = {
    ("runpod", "runpod-1"),
    ("runpod", "runpod-2"),
    ("vast", "vast-1"),
}
ACTIVE_PROVIDER_STATES = {
    "active",
    "running",
    "ready",
    "started",
}
ACTIVE_LOCAL_STATES = {
    "active",
    "created",
    "joining",
    "network_suspect",
    "setup",
    "starting",
    "watchdog",
}
ROLE_SET = {"head", "body", "tail"}
MAX_SOURCES = 3
MAX_ROWS = 150


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", text):
        raise ValueError(f"{name} is not a safe identifier")
    return text


def _timestamp(value: Any, name: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_source(raw: str) -> tuple[tuple[str, str], Path]:
    try:
        scope, raw_path = raw.split("=", 1)
        provider, account_scope = scope.split("/", 1)
    except ValueError as exc:
        raise ValueError("source must be PROVIDER/ACCOUNT_SCOPE=/absolute/path.json") from exc
    key = (provider.strip().lower(), account_scope.strip().lower())
    if key not in SUPPORTED_SCOPES:
        raise ValueError(f"unsupported provider/account scope: {scope}")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError("bootstrap evidence paths must be absolute")
    return key, path


def _source_map(values: list[str], name: str) -> dict[tuple[str, str], Path]:
    result: dict[tuple[str, str], Path] = {}
    for raw in values:
        key, path = _parse_source(raw)
        if key in result:
            raise ValueError(f"duplicate {name} source for {key[0]}/{key[1]}")
        result[key] = path
    if not result or len(result) > MAX_SOURCES:
        raise ValueError(f"{name} sources must contain between 1 and {MAX_SOURCES} scopes")
    return result


def _load(path: Path, name: str) -> dict[str, Any] | list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} source is unavailable or invalid: {path}") from exc
    if not isinstance(value, (dict, list)):
        raise ValueError(f"{name} source must contain a JSON object or array")
    return value


def _rows(value: dict[str, Any] | list[Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = value
    else:
        rows = next((value[key] for key in keys if isinstance(value.get(key), list)), None)
        if rows is None and isinstance(value.get("machines"), dict):
            rows = list(value["machines"].values())
        if rows is None:
            rows = []
    return [row for row in rows if isinstance(row, dict)]


def _document_time(value: dict[str, Any] | list[Any]) -> Any:
    if not isinstance(value, dict):
        return None
    return next(
        (value.get(key) for key in ("observedAt", "generatedAt", "refreshedAt", "updatedAt") if value.get(key)),
        None,
    )


def _resource_id(row: dict[str, Any], provider: str) -> str:
    keys = (
        ("resourceId", "providerResourceId", "runpodId", "podId", "id")
        if provider == "runpod"
        else ("resourceId", "providerResourceId", "vastInstanceId", "vastId", "contract_id", "id")
    )
    return str(next((row.get(key) for key in keys if row.get(key) is not None), "")).strip()


def _provider_state(row: dict[str, Any]) -> str:
    value = next(
        (row.get(key) for key in ("lifecycleState", "desiredStatus", "actual_status", "status", "state") if row.get(key) is not None),
        "",
    )
    return str(value).strip().lower()


def _gpu(row: dict[str, Any]) -> str:
    value = next(
        (row.get(key) for key in ("gpuModel", "gpuTypeId", "gpu_name", "gpuName", "gpu_type") if row.get(key)),
        "",
    )
    if not value:
        machine = row.get("machine")
        if isinstance(machine, dict):
            value = machine.get("gpuTypeId") or ""
    text = str(value).strip()
    if not text or len(text) > 128:
        raise ValueError("provider inventory GPU evidence is missing or invalid")
    return text


def _machine_name(row: dict[str, Any]) -> str | None:
    value = next((row.get(key) for key in ("name", "providerMachineName", "runpodName", "label") if row.get(key)), None)
    if value is None:
        return None
    text = str(value).strip()
    return text[:256] or None


def _fresh(observed_at: str, *, now: dt.datetime, freshness_seconds: int) -> bool:
    parsed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    age = (now - parsed).total_seconds()
    return 0 <= age <= freshness_seconds


def migration_ids(fleet_id: str, provider: str, account_scope: str, resource_id: str) -> dict[str, Any]:
    identity = f"{fleet_id}/{provider}/{account_scope}/{resource_id}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return {
        "launchId": f"migration-launch-{suffix}",
        "reservationId": f"migration-reservation-{suffix}",
        "slotId": f"migration-slot-{suffix}",
        "slotGeneration": 1,
        "machineGenerationId": f"migration-machine-{suffix}",
        "identityKind": "migration",
    }


def _quarantine(
    *, provider: str, account_scope: str, local_machine_id: str | None,
    resource_id: str | None, reason: str,
) -> dict[str, Any]:
    material = {
        "provider": provider,
        "accountScope": account_scope,
        "localMachineId": local_machine_id,
        "providerResourceId": resource_id,
        "reason": reason,
    }
    return {"quarantineId": f"bootstrap-quarantine-{_sha(material)[:24]}", **material}


def assemble_snapshot(
    *, fleet_id: str, expected_ledger_commit_seq: int,
    fleet_sources: list[str], inventory_sources: list[str], monitor_sources: list[str],
    freshness_seconds: int = 300, now: str | None = None,
    target_machine_id: str | None = None,
) -> dict[str, Any]:
    fleet_id = _identifier(fleet_id, "fleet id")
    if target_machine_id is not None:
        target_machine_id = _identifier(target_machine_id, "target machine id")
    if expected_ledger_commit_seq < 0:
        raise ValueError("expected ledger commit sequence must be non-negative")
    if not 30 <= freshness_seconds <= 3600:
        raise ValueError("freshness seconds must be from 30 to 3600")
    now_text = _timestamp(now or dt.datetime.now(dt.timezone.utc).isoformat(), "now")
    now_dt = dt.datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    fleets = _source_map(fleet_sources, "fleet")
    inventories = _source_map(inventory_sources, "inventory")
    monitors = _source_map(monitor_sources, "monitor")
    if set(fleets) != set(inventories) or set(fleets) != set(monitors):
        raise ValueError("fleet, inventory, and monitor sources must cover the same exact scopes")

    machines: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    evidence_sources: list[dict[str, Any]] = []
    target_matches = 0
    target_inactive = False
    for provider, account_scope in sorted(fleets):
        fleet_path, inventory_path, monitor_path = fleets[(provider, account_scope)], inventories[(provider, account_scope)], monitors[(provider, account_scope)]
        fleet_doc = _load(fleet_path, "fleet")
        inventory_doc = _load(inventory_path, "inventory")
        monitor_doc = _load(monitor_path, "monitor")
        monitor_time = _timestamp(_document_time(monitor_doc), "monitor generatedAt")
        if not isinstance(monitor_doc, dict) or monitor_doc.get("readOnly") is not True:
            raise ValueError(f"monitor source for {provider}/{account_scope} is not read-only evidence")
        if not _fresh(monitor_time, now=now_dt, freshness_seconds=freshness_seconds):
            raise ValueError(f"monitor source for {provider}/{account_scope} is stale")
        evidence_sources.append({
            "provider": provider,
            "accountScope": account_scope,
            "fleetSha256": hashlib.sha256(fleet_path.read_bytes()).hexdigest(),
            "inventorySha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            "monitorSha256": hashlib.sha256(monitor_path.read_bytes()).hexdigest(),
            "monitorObservedAt": monitor_time,
        })

        inventory_by_id: dict[str, dict[str, Any]] = {}
        inventory_conflicts: set[str] = set()
        for row in _rows(inventory_doc, ("items", "pods", "instances", "resources", "rows")):
            resource_id = _resource_id(row, provider)
            if not resource_id:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=None, resource_id=None, reason="provider_resource_identity_missing"))
                continue
            state = _provider_state(row)
            active = state in ACTIVE_PROVIDER_STATES or (provider == "runpod" and state == "running")
            if not active:
                continue
            if resource_id in inventory_conflicts:
                continue
            if resource_id in inventory_by_id:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=None, resource_id=resource_id, reason="duplicate_provider_inventory_identity"))
                inventory_by_id.pop(resource_id, None)
                inventory_conflicts.add(resource_id)
                continue
            inventory_by_id[resource_id] = row

        monitor_rows = _rows(monitor_doc, ("results", "items", "rows"))
        monitor_by_machine: dict[str, dict[str, Any]] = {}
        monitor_conflicts: set[str] = set()
        for monitor_row in monitor_rows:
            monitor_machine_id = str(monitor_row.get("machineId") or "").strip()
            if not monitor_machine_id or monitor_machine_id in monitor_conflicts:
                continue
            if monitor_machine_id in monitor_by_machine:
                monitor_by_machine.pop(monitor_machine_id, None)
                monitor_conflicts.add(monitor_machine_id)
                continue
            monitor_by_machine[monitor_machine_id] = monitor_row
        fleet_rows = _rows(fleet_doc, ("machines", "items", "rows"))
        if target_machine_id is not None:
            target_rows = [
                row for row in fleet_rows
                if str(row.get("id") or row.get("machineId") or "").strip() == target_machine_id
            ]
            if len(target_rows) > 1:
                raise ValueError(f"target machine identity is not unique: {target_machine_id}")
            if target_rows:
                target_matches += 1
                target_inactive = target_inactive or (
                    str(target_rows[0].get("status") or "active").strip().lower()
                    not in ACTIVE_LOCAL_STATES
                )
            fleet_rows = target_rows
        matched_provider_ids: set[str] = set()
        for row in fleet_rows:
            machine_id = str(row.get("id") or row.get("machineId") or "").strip()
            resource_id = _resource_id(row, provider)
            if not machine_id or not resource_id:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id or None, resource_id=resource_id or None, reason="local_identity_missing"))
                continue
            if str(row.get("status") or "active").strip().lower() not in ACTIVE_LOCAL_STATES:
                continue
            inventory = inventory_by_id.get(resource_id)
            if inventory is None:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason="provider_resource_not_currently_active"))
                continue
            matched_provider_ids.add(resource_id)
            if machine_id in monitor_conflicts:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason="conflicting_ssh_monitor_evidence"))
                continue
            monitor = monitor_by_machine.get(machine_id)
            if monitor is None or monitor.get("sshReturncode") != 0:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason="fresh_ssh_monitor_evidence_missing"))
                continue
            role_evidence = monitor.get("joinEvidence")
            role = str(role_evidence.get("role") if isinstance(role_evidence, dict) else "").strip().lower()
            if role not in ROLE_SET or not isinstance(role_evidence, dict) or role_evidence.get("source") != "ssh_monitor_log":
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason="ssh_role_evidence_missing"))
                continue
            boot_id = str(monitor.get("bootId") or "").strip()
            if not boot_id or monitor.get("bootIdSource") != "ssh_procfs":
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason="ssh_boot_id_evidence_missing"))
                continue
            qualified = f"{provider}/{account_scope}/{resource_id}"
            try:
                gpu_model = _gpu(inventory)
                provider_observed_at = _timestamp(
                    next((inventory.get(key) for key in ("observedAt", "sourceUpdatedAt", "updatedAt") if inventory.get(key)), _document_time(inventory_doc)),
                    "provider inventory observedAt",
                )
                if not _fresh(provider_observed_at, now=now_dt, freshness_seconds=freshness_seconds):
                    raise ValueError("provider inventory evidence is stale")
                _identifier(machine_id, "local machine id")
                _identifier(resource_id, "provider resource id")
                _identifier(boot_id, "SSH boot id")
            except ValueError as exc:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=machine_id, resource_id=resource_id, reason=str(exc).replace(" ", "_").lower()[:128]))
                continue
            machines.append({
                **migration_ids(fleet_id, provider, account_scope, resource_id),
                "provider": provider,
                "accountScope": account_scope,
                "providerResourceId": resource_id,
                "providerQualifiedResourceId": qualified,
                "providerMachineName": _machine_name(inventory) or _machine_name(row),
                "gpuModel": gpu_model,
                "providerObservedAt": provider_observed_at,
                "monitorObservedAt": monitor_time,
                "localMachineId": machine_id,
                "bootId": boot_id,
                "nodeType": role,
                "joinState": "joined",
                "lifecycleState": "running",
                "provisioningOrigin": "read_model_migration",
                "evidence": {
                    "provider": "provider_inventory",
                    "role": "ssh_monitor_log",
                    "boot": "ssh_procfs",
                    "roleEvidenceSha256": hashlib.sha256(
                        str(role_evidence.get("evidence") or "").encode("utf-8")
                    ).hexdigest(),
                },
            })
        for resource_id in sorted(set(inventory_by_id) - matched_provider_ids):
            if target_machine_id is None:
                quarantined.append(_quarantine(provider=provider, account_scope=account_scope, local_machine_id=None, resource_id=resource_id, reason="active_provider_resource_not_registered_locally"))

    if target_machine_id is not None:
        if target_matches != 1:
            raise ValueError(f"target machine identity is not present exactly once: {target_machine_id}")
        if target_inactive:
            raise ValueError(f"target machine is inactive: {target_machine_id}")

    local_counts: dict[str, int] = {}
    qualified_counts: dict[str, int] = {}
    for machine in machines:
        local_counts[machine["localMachineId"]] = local_counts.get(machine["localMachineId"], 0) + 1
        qualified_counts[machine["providerQualifiedResourceId"]] = qualified_counts.get(machine["providerQualifiedResourceId"], 0) + 1
    eligible: list[dict[str, Any]] = []
    for machine in machines:
        reason = None
        if local_counts[machine["localMachineId"]] > 1:
            reason = "cross_scope_local_machine_identity_conflict"
        elif qualified_counts[machine["providerQualifiedResourceId"]] > 1:
            reason = "provider_qualified_identity_conflict"
        if reason:
            quarantined.append(_quarantine(
                provider=machine["provider"], account_scope=machine["accountScope"],
                local_machine_id=machine["localMachineId"],
                resource_id=machine["providerResourceId"], reason=reason,
            ))
        else:
            eligible.append(machine)
    machines = eligible

    if len(machines) + len(quarantined) > MAX_ROWS:
        raise ValueError(f"bootstrap snapshot exceeds the {MAX_ROWS}-row bound")
    machines.sort(key=lambda row: row["providerQualifiedResourceId"])
    quarantined = sorted({row["quarantineId"]: row for row in quarantined}.values(), key=lambda row: row["quarantineId"])
    material = {
        "schemaVersion": SCHEMA_VERSION,
        "fleetId": fleet_id,
        "collectedAt": now_text,
        "expectedLedgerCommitSeq": expected_ledger_commit_seq,
        "machines": machines,
        "quarantined": quarantined,
        "evidenceSources": evidence_sources,
    }
    return {**material, "snapshotChecksum": _sha(material)}


def _fleet_machine_rows(document: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return [row for row in document if isinstance(row, dict)]
    machines = document.get("machines") if isinstance(document, dict) else None
    if isinstance(machines, dict):
        return [row for row in machines.values() if isinstance(row, dict)]
    if isinstance(machines, list):
        return [row for row in machines if isinstance(row, dict)]
    return _rows(document, ("items", "rows"))


def _atomic_write_json(path: Path, value: dict[str, Any] | list[Any]) -> None:
    mode = path.stat().st_mode
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def reconcile_local_identities(
    *, snapshot: dict[str, Any], fleet_sources: list[str], committed: dict[str, Any],
) -> dict[str, Any]:
    receipt = committed.get("receipt") if isinstance(committed, dict) else None
    safe_receipt = (
        committed.get("accepted") is True
        and committed.get("executable") is True
        and committed.get("snapshotChecksum") == snapshot["snapshotChecksum"]
        and isinstance(receipt, dict)
        and receipt.get("snapshotChecksum") == snapshot["snapshotChecksum"]
        and receipt.get("fleetId") == snapshot["fleetId"]
        and receipt.get("machineCount") == len(snapshot["machines"])
        and receipt.get("authorityChanged") is False
        and receipt.get("providerMutationAuthorityChanged") is False
        and receipt.get("machineCommandAuthorityChanged") is False
    )
    if not safe_receipt:
        raise ValueError("Cloud Fleet bootstrap commit receipt is absent, mismatched, or changed authority")

    source_paths = {
        scope: path.resolve(strict=True)
        for scope, path in _source_map(fleet_sources, "fleet").items()
    }
    originals: dict[Path, dict[str, Any] | list[Any]] = {}
    updated: dict[Path, dict[str, Any] | list[Any]] = {}
    changed_paths: set[Path] = set()
    reconciled_ids: list[str] = []
    committed_at = _timestamp(receipt.get("committedAt"), "bootstrap committedAt")
    for path in source_paths.values():
        if path not in originals:
            originals[path] = _load(path, "fleet")
            updated[path] = json.loads(json.dumps(originals[path]))

    canonical_fields = (
        "fleetId", "launchId", "reservationId", "slotId", "slotGeneration",
        "machineGenerationId", "provider", "accountScope", "cloudProviderResourceId",
        "gpuModel", "agoraJoinRole", "cloudBootId",
        "fleetBootstrapSnapshotChecksum", "fleetBootstrapCommittedAt",
    )
    for machine in snapshot["machines"]:
        scope = (machine["provider"], machine["accountScope"])
        path = source_paths.get(scope)
        if path is None:
            raise ValueError(f"committed machine has no local fleet source for {scope[0]}/{scope[1]}")
        candidates = [
            row for row in _fleet_machine_rows(updated[path])
            if str(row.get("id") or row.get("machineId") or "").strip() == machine["localMachineId"]
        ]
        if len(candidates) != 1:
            raise ValueError(f"local machine identity is no longer unique: {machine['localMachineId']}")
        row = candidates[0]
        if _resource_id(row, machine["provider"]) != machine["providerResourceId"]:
            raise ValueError(f"provider identity changed before local reconciliation: {machine['localMachineId']}")
        values = {
            "fleetId": snapshot["fleetId"],
            "launchId": machine["launchId"],
            "reservationId": machine["reservationId"],
            "slotId": machine["slotId"],
            "slotGeneration": machine["slotGeneration"],
            "machineGenerationId": machine["machineGenerationId"],
            "provider": machine["provider"],
            "accountScope": machine["accountScope"],
            "cloudProviderResourceId": f"{machine['provider']}:{machine['accountScope']}:{machine['providerResourceId']}",
            "gpuModel": machine["gpuModel"],
            "agoraJoinRole": machine["nodeType"],
            "cloudBootId": machine["bootId"],
            "fleetBootstrapSnapshotChecksum": snapshot["snapshotChecksum"],
            "fleetBootstrapCommittedAt": committed_at,
        }
        for field in canonical_fields:
            if row.get(field) is not None and row[field] != values[field]:
                raise ValueError(f"local canonical identity conflicts at {machine['localMachineId']}.{field}")
        row.update(values)
        changed_paths.add(path)
        reconciled_ids.append(machine["localMachineId"])

    written: list[Path] = []
    try:
        for path in sorted(changed_paths, key=str):
            _atomic_write_json(path, updated[path])
            written.append(path)
    except Exception:
        for path in written:
            _atomic_write_json(path, originals[path])
        raise
    return {
        "snapshotChecksum": snapshot["snapshotChecksum"],
        "machineCount": len(reconciled_ids),
        "machineIds": sorted(reconciled_ids),
        "sourceCount": len(changed_paths),
    }


def run_fleet_read_model_bootstrap(
    args: Any, *, client_factory: Callable[[], Any], fleet_error: type[Exception]
) -> dict[str, Any]:
    execute = bool(getattr(args, "execute", False))
    yes = bool(getattr(args, "yes", False))
    if execute != yes:
        raise fleet_error("fleet read-model bootstrap commit requires --execute and --yes together")
    try:
        snapshot = assemble_snapshot(
            fleet_id=args.fleet_id,
            expected_ledger_commit_seq=int(args.expected_ledger_commit_seq),
            fleet_sources=list(args.fleet_source or []),
            inventory_sources=list(args.inventory_source or []),
            monitor_sources=list(args.monitor_source or []),
            freshness_seconds=int(args.freshness_seconds),
            now=getattr(args, "now", None),
        )
    except ValueError as exc:
        raise fleet_error(str(exc)) from exc
    result = client_factory().bootstrap_fleet_read_model(snapshot, execute=execute)
    if not execute:
        return result
    try:
        reconciliation = reconcile_local_identities(
            snapshot=snapshot, fleet_sources=list(args.fleet_source or []), committed=result,
        )
    except (OSError, ValueError) as exc:
        raise fleet_error(f"Cloud bootstrap committed, but local identity reconciliation failed: {exc}") from exc
    return {**result, "localIdentityReconciliation": reconciliation}
