"""Reproducible metadata features in SQLite, refreshed from catalog facts."""

from sqlalchemy import text

from app.operational_state import configured_store

FEATURE_SCOPE = "library.collection_features"


def feature_from_schema(md5, schema):
    from app.modules.library.collection_detection import build_document_features, CollectionEligibilityPolicy
    feature = build_document_features(md5, schema if isinstance(schema, dict) else {})
    decision = CollectionEligibilityPolicy().evaluate(schema)
    feature.update(eligible=decision.eligible, exclusion_reason=decision.reason if not decision.eligible else "")
    for field in ("publishers", "authors", "genres", "series_hints"):
        feature[field + "_json"] = feature[field]
    return feature


def load_features(conn, md5s):
    """Refresh by content hash; missing/stale local rows never become truth."""
    if not md5s:
        return {}
    rows = conn.execute(text("SELECT md5,schema_org FROM metadata WHERE md5=ANY(:md5s)"),
                        {"md5s": sorted(set(md5s))}).mappings()
    store = configured_store()
    result = {}
    with store.transaction() as local:
        for row in rows:
            feature = feature_from_schema(row["md5"], row["schema_org"])
            result[row["md5"]] = feature
            store.put(FEATURE_SCOPE, row["md5"], feature, conn=local)
    return result


def attach_features(conn, rows):
    features = load_features(conn, [row["md5"] for row in rows])
    result = []
    for row in rows:
        feature = features.get(row["md5"])
        if feature is None:
            raise ValueError("Collection proposal document is absent; rerun detection before review")
        if row.get("input_hash") and row["input_hash"] != feature["input_hash"]:
            raise ValueError("Collection proposal metadata changed; rerun detection before review")
        result.append({**feature, **dict(row)})
    return result
