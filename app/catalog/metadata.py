"""Lossless relational representation of the supported bibliographic envelope.

JSON objects here are transport and source evidence, never authoritative storage.
"""

from copy import deepcopy
import re
from typing import Any

from app.catalog.schema_org import metadata_contract_issues


SCALARS = {
    "@type": "work_type", "name": "name", "description": "description",
    "datePublished": "date_published", "numberOfPages": "page_count", "bookEdition": "edition",
}
RELATIONS = {"author", "editor", "translator", "illustrator", "publisher", "contributor"}
FIELDS = set(SCALARS) | RELATIONS | {
    "@context", "inLanguage", "isbn", "about", "genre", "audience", "accessMode",
    "accessModeSufficient", "isBasedOn",
}


def items(value: Any) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def decompose_metadata(source: dict) -> dict:
    if not isinstance(source, dict) or set(source) - FIELDS:
        raise ValueError("unsupported bibliographic properties; retain the original as evidence")
    if source.get("@context", "https://schema.org") != "https://schema.org":
        raise ValueError("unsupported schema context")
    try:
        issues = metadata_contract_issues({"@context": "https://schema.org", **source})
    except (TypeError, AttributeError) as exc:
        raise ValueError("unsupported bibliographic values") from exc
    if issues:
        raise ValueError("unsupported bibliographic values: " + ", ".join(sorted({item["code"] for item in issues})))
    scalars = {column: source[key] for key, column in SCALARS.items() if key in source}
    for key, value in scalars.items():
        if key == "page_count":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("page_count must be a positive integer")
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be text")
    if "date_published" in scalars and not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", scalars["date_published"]):
        raise ValueError("date_published must preserve year/month/day precision")
    credits = []
    for role in sorted(RELATIONS):
        for position, item in enumerate(items(source.get(role))):
            nested = item
            custom_role = None
            if isinstance(item, dict) and item.get("@type") == "Role":
                custom_role = item.get("roleName")
                nested = item.get("contributor")
            for nested_position, entity in enumerate(items(nested)):
                if not isinstance(entity, dict) or entity.get("@type") not in {"Person", "Organization"}:
                    raise ValueError("unsupported credited entity")
                if set(entity) - {"@type", "name"} or not isinstance(entity.get("name"), str):
                    raise ValueError("unsupported credited entity fields")
                credits.append({"role": role, "role_name": custom_role, "position": position,
                                "nested_position": nested_position, "raw_name": entity["name"],
                                "kind": entity["@type"].lower()})
    language = source.get("inLanguage") or ""
    if not isinstance(language, str):
        raise ValueError("inLanguage must be text")
    return {
        "scalars": scalars, "languages": [part.strip() for part in language.split(",") if part.strip()],
        "credits": credits, "identifiers": items(source.get("isbn")),
        "genres": items(source.get("genre")), "subjects": deepcopy(items(source.get("about"))),
        "audiences": deepcopy(items(source.get("audience"))),
        "audience_array": isinstance(source.get("audience"), list),
        "access_modes": items(source.get("accessMode")),
        "sufficient_modes": [item["itemListElement"] for item in items(source.get("accessModeSufficient"))],
        "based_on": deepcopy(source.get("isBasedOn")),
    }


def compose_metadata(record: dict) -> dict:
    result = {"@context": "https://schema.org"}
    result.update({key: record["scalars"][column] for key, column in SCALARS.items()
                   if record["scalars"].get(column) is not None})
    if record.get("languages"):
        result["inLanguage"] = ",".join(record["languages"])
    groups: dict[tuple, list] = {}
    for credit in record.get("credits", []):
        key = (credit["role"], credit["position"], credit.get("role_name"))
        groups.setdefault(key, []).append({"@type": credit["kind"].title(), "name": credit["raw_name"]})
    for (role, _position, custom_role), entities in sorted(groups.items(), key=lambda pair: (pair[0][0], pair[0][1])):
        value = entities[0] if len(entities) == 1 else entities
        if custom_role:
            value = {"@type": "Role", "roleName": custom_role, "contributor": value}
        if role == "publisher":
            result[role] = value
        else:
            result.setdefault(role, []).append(value)
    for field, key in {"isbn": "identifiers", "genre": "genres", "about": "subjects", "accessMode": "access_modes"}.items():
        if record.get(key):
            result[field] = deepcopy(record[key])
    if record.get("audiences"):
        result["audience"] = deepcopy(record["audiences"] if record.get("audience_array") else record["audiences"][0])
    if record.get("sufficient_modes"):
        result["accessModeSufficient"] = [{"@type": "ItemList", "itemListElement": modes} for modes in record["sufficient_modes"]]
    if record.get("based_on") is not None:
        result["isBasedOn"] = deepcopy(record["based_on"])
    return result
