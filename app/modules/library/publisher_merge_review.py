"""Strict review intents for server-owned publisher drafts."""

from app.modules.library.publisher_workbench import _id, _name, validate_change_set


def save_draft(db, payload):
    if (
        not isinstance(payload, dict)
        or set(payload) - {"revision", "changes", "reviewed"}
        or not {"revision", "changes"}.issubset(payload)
    ):
        raise ValueError("draft requires revision and changes")
    revision = payload["revision"]
    if type(revision) is not int or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    reviewed = payload.get("reviewed")
    if reviewed is not None:
        if not isinstance(reviewed, dict):
            raise ValueError("reviewed must be an object")
        for key, value in reviewed.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, dict)
                or set(value) != {"display_name", "aliases", "is_new"}
            ):
                raise ValueError("invalid reviewed publisher snapshot")
            if (
                not isinstance(value["display_name"], str)
                or type(value["is_new"]) is not bool
                or not isinstance(value["aliases"], list)
                or any(not isinstance(name, str) for name in value["aliases"])
            ):
                raise ValueError("invalid reviewed publisher snapshot fields")
    return db.save_publisher_draft(
        validate_change_set(payload["changes"]), revision, reviewed
    )


def discard_draft(db, payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise ValueError("revision must be a non-negative integer")
    return db.discard_publisher_draft(payload["revision"])


def review_action(db, proposal_id, payload):
    _id(proposal_id, "proposal_id")
    if not isinstance(payload, dict) or set(payload) - {
        "action",
        "member_ids",
        "display_name",
    }:
        raise ValueError("unsupported review fields")
    action = payload.get("action")
    if action not in {"stage", "separate", "skip", "edit"}:
        raise ValueError("action must be stage, separate, skip or edit")
    members = payload.get("member_ids")
    if (
        not isinstance(members, list)
        or any(not isinstance(key, str) for key in members)
        or len(set(members)) != len(members)
    ):
        raise ValueError("member_ids must be distinct strings")
    if len(members) < (2 if action == "separate" else 1):
        raise ValueError("review needs at least one member; separation needs two")
    value = payload.get("display_name", "")
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("display_name must be a string of at most 240 characters")
    display_name = _name(value) if action in {"stage", "edit"} else value.strip()
    return db.publisher_review_action(proposal_id, action, members, display_name)
