"""Low-priority filesystem observations and explicit occurrence reconciliation.

Observing bytes never claims a user's placement. The observation remains in
the journal so late metadata can absorb it without publishing a compensating
delete or manufacturing a second occurrence.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib

from solin.core.ingest.sync.resources import resource_identity_key


RESOURCE = "__sync_resource"
DISCOVERED = "__sync_discovered"
AUXILIARY = "__sync_auxiliary"
SUPPRESSED = "__sync_suppressed_resource"
OVERRIDE_PREFIX = "__sync_override:"
ACKNOWLEDGED = "__sync_acknowledged_deletions"
REPEATABLE = "__sync_repeatable"


def _same_field(left: dict, right: dict, key: str) -> bool:
    return (key in left) == (key in right) and left.get(key) == right.get(key)


def _copy_field(source: dict, destination: dict, key: str) -> None:
    if key in source:
        destination[key] = deepcopy(source[key])
    else:
        destination.pop(key, None)


def _record_override(source: dict, destination: dict, key: str) -> None:
    # One register per field preserves independent concurrent changes. A map
    # containing all overrides would itself be a single last-writer register.
    destination[OVERRIDE_PREFIX + key] = (
        {"present": True, "value": deepcopy(source[key])}
        if key in source else {"present": False}
    )


def suppression_record(node_id: str, resource: str, *, automatic: bool = False) -> tuple[str, dict]:
    digest = hashlib.sha256(f"{node_id}\0{resource_identity_key(resource)}".encode()).hexdigest()
    return f"$deleted-resource:{digest}", {
        AUXILIARY: True,
        SUPPRESSED: resource,
        "occurrence_id": node_id,
        "automatic": automatic,
    }


def suppressed_resources(entities: Mapping[str, dict]) -> set[str]:
    return {
        resource_identity_key(str(value[SUPPRESSED]))
        for value in entities.values()
        if value.get(SUPPRESSED)
    }


def reconcile_discoveries(entities: Mapping[str, dict]) -> dict[str, dict]:
    """Project observations onto explicit occurrences without changing history."""
    result = deepcopy(dict(entities))
    explicit: dict[str, list[str]] = {}
    suppressed = suppressed_resources(entities)
    for node_id, node in entities.items():
        resource = resource_identity_key(str(node.get(RESOURCE) or ""))
        if resource and not node.get(DISCOVERED) and not node.get(AUXILIARY):
            explicit.setdefault(resource, []).append(node_id)
    for ids in explicit.values():
        ids.sort()
    for node_id, node in entities.items():
        if not node.get(DISCOVERED):
            continue
        resource = resource_identity_key(str(node.get(RESOURCE) or ""))
        claims = explicit.get(resource, [])
        if claims:
            target = result[claims[0]]
            for field, intent in node.items():
                if not field.startswith(OVERRIDE_PREFIX):
                    continue
                key = field.removeprefix(OVERRIDE_PREFIX)
                if intent["present"]:
                    target[key] = deepcopy(intent["value"])
                else:
                    target.pop(key, None)
        if claims or resource in suppressed:
            result.pop(node_id, None)
    # Removing a provisional occurrence also removes its subsequently delivered
    # primary claim. A deliberate re-add acknowledges the observed deletion.
    for deletion_id, deletion in entities.items():
        if not deletion.get(SUPPRESSED):
            continue
        claims = explicit.get(resource_identity_key(str(deletion[SUPPRESSED])), [])
        unacknowledged = [key for key in claims
                          if deletion_id not in entities[key].get(ACKNOWLEDGED, [])]
        for index, claim in enumerate(unacknowledged):
            if not entities[claim].get(REPEATABLE) or (deletion.get("automatic") and index == 0):
                result.pop(claim, None)
    # Preserve the existing domain occurrence policy across concurrent writers:
    # independently discovering/adding the same image is still one occurrence;
    # explicit repeatable video occurrences remain distinct.
    for claims in explicit.values():
        visible_claims = [key for key in claims if key in result]
        for key in visible_claims[1:]:
            if not result[key].get(REPEATABLE):
                result.pop(key, None)
    return result


def record_discovery_edits(
    base: Mapping[str, dict],
    desired: Mapping[str, dict],
) -> dict[str, dict]:
    """Translate edits of the visible projection back to durable intentions."""
    result = deepcopy(dict(desired))
    visible = reconcile_discoveries(base)
    for node_id, node in base.items():
        if node.get(AUXILIARY) or node_id not in visible:
            result.setdefault(node_id, deepcopy(node))
            if node.get(DISCOVERED) and node_id not in visible:
                # Continue editing the same per-field intentions after the
                # explicit claim absorbs the observation. Clearing every
                # override here would discard concurrent edits of unrelated
                # fields on a terminal still displaying the fallback.
                claims = [
                    key
                    for key, candidate in visible.items()
                    if resource_identity_key(str(candidate.get(RESOURCE) or ""))
                    == resource_identity_key(str(node.get(RESOURCE) or ""))
                    and not candidate.get(DISCOVERED)
                    and not candidate.get(AUXILIARY)
                ]
                if claims and min(claims) in desired:
                    claim = min(claims)
                    before, after = visible[claim], desired[claim]
                    overridden = {field.removeprefix(OVERRIDE_PREFIX) for field in node
                                  if field.startswith(OVERRIDE_PREFIX)}
                    for key in before.keys() | after.keys() | overridden:
                        if key.startswith(OVERRIDE_PREFIX) or key in {RESOURCE, DISCOVERED}:
                            continue
                        changed = not _same_field(before, after, key)
                        if changed:
                            _record_override(after, result[node_id], key)
                            _copy_field(after, result[node_id], key)
                        if changed or OVERRIDE_PREFIX + key in node:
                            # Projection is not an edit of the original claim.
                            _copy_field(base[claim], result[claim], key)
    for node_id, before in visible.items():
        if before.get(AUXILIARY):
            continue
        after = result.get(node_id)
        if after is None:
            resource = str(before.get(RESOURCE) or "")
            if resource:
                key, record = suppression_record(
                    node_id,
                    resource,
                    automatic=bool(before.get(DISCOVERED)),
                )
                result[key] = record
            continue
        if before.get(DISCOVERED):
            # A GUI view predating local staging does not yet carry newly
            # recorded intention registers. Replaying that view must preserve
            # the registers already accepted in its causal snapshot.
            for key, value in before.items():
                if key.startswith(OVERRIDE_PREFIX):
                    after.setdefault(key, deepcopy(value))
            for key in before.keys() | after.keys():
                if key.startswith(OVERRIDE_PREFIX) or key in {RESOURCE, DISCOVERED}:
                    continue
                if _same_field(before, after, key):
                    continue
                _record_override(after, after, key)
    for node_id, node in result.items():
        if node_id in base or node.get(DISCOVERED) or node.get(AUXILIARY):
            continue
        resource = resource_identity_key(str(node.get(RESOURCE) or ""))
        acknowledged = sorted(
            key
            for key, value in base.items()
            if resource_identity_key(str(value.get(SUPPRESSED) or "")) == resource and resource
        )
        if acknowledged:
            node[ACKNOWLEDGED] = acknowledged
    return result
