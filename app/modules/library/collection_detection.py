"""Deterministic publication-based collection candidates; no catalog mutations."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яёәҗңөүһіғқҫ]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")
_ISSUE_RE = re.compile(
    r"(?i)(?:^|\s)(?:№|#|issue|vol(?:ume)?|том|выпуск|сан|number|num)\s*[\w.-]+(?=[\s,;:]|$)[,;:]?"
)
_TAIL_NUMBER_RE = re.compile(
    r"[\s._-](?:\d{1,4}|[ivxlcdm]{1,8})(?:[\s._-]*\d{0,4})?$", re.I
)

LEGAL_GENRE_BLACKLIST = frozenset(
    {
        "legislation",
        "law",
        "laws",
        "legal",
        "legal act",
        "legal acts",
        "legal document",
        "legal documents",
        "legal code",
        "legal draft",
        "legal amendments",
        "local legislation",
        "administrative legislation",
        "draft law",
        "federal law",
        "decree",
        "decrees",
        "government decree",
        "governmental decree",
        "local decree",
        "municipal decree",
        "administrative decree",
        "official decree",
        "resolution",
        "resolutions",
        "government resolution",
        "local government resolution",
        "municipal resolution",
        "administrative resolution",
        "official resolution",
        "regulation",
        "regulations",
        "administrative regulation",
        "administrative regulations",
        "government regulation",
        "local regulation",
        "regulatory document",
        "rules and regulations",
        "statute",
        "statutes",
        "charter",
        "charters",
        "constitution",
        "ordinance",
        "ordinances",
        "administrative act",
        "administrative document",
        "administrative documents",
        "administrative order",
        "official document",
        "official documents",
        "government document",
        "court ruling",
        "закон",
        "указ",
        "постановление",
        "распоряжение",
        "приказ",
        "административ регламент",
        "карар",
        "карар проекты",
        "боерык",
        "хокукый акт",
        "норматив хокукый акт",
        "рәсми документ",
    }
)


def normalize_collection_text(value: Any) -> str:
    normalized = _PUNCT_RE.sub(" ", str(value or "").casefold())
    return _SPACE_RE.sub(" ", normalized).strip()


def title_core(value: str) -> str:
    stripped = _ISSUE_RE.sub(" ", value or "")
    stripped = _YEAR_RE.sub(" ", stripped)
    stripped = _TAIL_NUMBER_RE.sub("", stripped)
    return normalize_collection_text(stripped)


def _normalized_genre_is_legal(value: str) -> bool:
    normalized = normalize_collection_text(value)
    if normalized in LEGAL_GENRE_BLACKLIST:
        return True
    tokens = set(normalized.split())
    if tokens & {
        "legislation",
        "decree",
        "decrees",
        "resolution",
        "resolutions",
        "regulation",
        "regulations",
    }:
        return True
    return any(
        phrase in normalized
        for phrase in (
            "government decree",
            "administrative regulation",
            "legal document",
            "норматив хокукый акт",
        )
    )


def cluster_near_title_cores(cores: Iterable[str], *, should_stop) -> dict[str, str]:
    """Join title cores that differ by one insertion, deletion, or substitution."""
    unique = sorted({value for value in cores if len(value) >= 6})
    parents = {value: value for value in unique}

    def find(value: str) -> str:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    signatures: dict[str, str] = {}
    for value in unique:
        should_stop()
        # The full value links a one-character insertion to the shorter title.
        for signature in {
            value,
            *(value[:index] + value[index + 1 :] for index in range(len(value))),
        }:
            existing = signatures.get(signature)
            if existing is None:
                signatures[signature] = value
            else:
                union(value, existing)
    return {value: find(value) for value in unique}


def build_publication_features(record: dict) -> dict:
    """Extract represented catalog facts without reconstructing Schema.org."""
    title = str(record["name"] or "").strip()
    genres = record["genres"]
    reason = "eligible"
    if not record["has_metadata"] or not record["metadata_present"]:
        reason = "missing_metadata"
    elif not title:
        reason = "missing_title"
    elif normalize_collection_text(record["work_type"]) == "legislation":
        reason = "excluded_work_type"
    elif any(_normalized_genre_is_legal(value) for value in genres):
        reason = "excluded_genre"
    issues = [subject["term_code"] or subject["name"] for subject in record["subjects"]
              if normalize_collection_text(subject["set_name"]) in {"issuenumber", "issue number"}]
    publisher_keys = sorted({
        f"entity:{credit['confirmed_entity_id']}" if credit["confirmed_entity_id"] is not None
        else f"name:{credit['name_id']}"
        for credit in record["credits"] if credit["role"] == "publisher"
    })
    return {
        "publication_id": record["publication_id"], "title": title,
        "title_core": title_core(title), "work_type": record["work_type"] or "",
        "genres": genres, "publisher_keys": publisher_keys,
        "has_issue_marker": bool(any(issues) or _ISSUE_RE.search(title)
                                 or _YEAR_RE.search(title) or _TAIL_NUMBER_RE.search(title)),
        "collection_id": record["collection_id"], "eligible": reason == "eligible",
        "exclusion_reason": reason, "snapshot": record,
    }


def collection_signatures(features: list[dict], collections: list[dict]) -> list[dict]:
    """Canonical titles and existing member titles are the only match signatures."""
    signatures = {row["collection_id"]: {title_core(row["title"])} for row in collections}
    for item in features:
        if item["collection_id"] in signatures and item["title_core"]:
            signatures[item["collection_id"]].add(item["title_core"])
    return [{"collection_id": key, "value": value}
            for key, values in sorted(signatures.items()) for value in sorted(values) if value]


def _candidate_score(items: list[dict]) -> tuple[float, dict]:
    marker_ratio = sum(item["has_issue_marker"] for item in items) / len(items)
    periodical_ratio = sum(
        normalize_collection_text(item["work_type"]) in {"newsarticle", "periodical"}
        or any(any(word in normalize_collection_text(genre) for word in ("newspaper", "periodical", "journal"))
               for genre in item["genres"])
        for item in items
    ) / len(items)
    publishers = Counter(key for item in items for key in item["publisher_keys"])
    publisher_ratio = max(publishers.values(), default=0) / sum(publishers.values()) if publishers else 0.0
    score = min(0.99, 0.45 + min(0.15, len(items) * 0.015)
                + marker_ratio * 0.15 + periodical_ratio * 0.15 + publisher_ratio * 0.08)
    return round(score, 4), {
        "marker_ratio": round(marker_ratio, 4), "periodical_ratio": round(periodical_ratio, 4),
        "publisher_ratio": round(publisher_ratio, 4),
    }


def discover_collections(features: list[dict], collections: list[dict], matches: list[dict],
                         *, should_stop, progress) -> tuple[list[dict], dict]:
    """Produce review candidates without AI, membership writes, or inclusion decisions."""
    counters = Counter(scanned=len(features))
    for item in features:
        counters["eligible" if item["eligible"] else "excluded"] += 1
        if not item["eligible"]:
            counters["excluded_" + item["exclusion_reason"]] += 1
    by_id = {item["publication_id"]: item for item in features}
    collection_by_id = {row["collection_id"]: row for row in collections}
    by_collection = defaultdict(list)
    matched = set()
    for match in matches:
        should_stop()
        item = by_id[match["publication_id"]]
        by_collection[match["collection_id"]].append({**item, "match": match})
        matched.add(item["publication_id"])
    proposals = []
    for collection_id, items in sorted(by_collection.items()):
        should_stop()
        proposals.append({
            "proposal_type": "attach_to_collection", "target": collection_by_id[collection_id],
            "title": collection_by_id[collection_id]["title"],
            "score": round(min(item["match"]["score"] for item in items), 4),
            "evidence": {"match": "canonical_signature", "matches": [item["match"] for item in items]},
            "members": [item["snapshot"] for item in items],
        })
        counters["attachment_proposals"] += 1
        counters["attachment_publications"] += len(items)
        counters["ambiguous_attachments"] += sum(item["match"]["conflicting_collection_ids"] != [] for item in items)
    unmatched = [item for item in features if item["eligible"] and item["collection_id"] is None
                 and item["publication_id"] not in matched and item["title_core"]]
    clusters = cluster_near_title_cores((item["title_core"] for item in unmatched), should_stop=should_stop)
    groups = defaultdict(list)
    for item in unmatched:
        should_stop()
        groups[clusters.get(item["title_core"], item["title_core"])].append(item)
    for core, items in sorted(groups.items()):
        should_stop()
        if len(items) < 2 or len(core) < 4:
            continue
        score, evidence = _candidate_score(items)
        if max(evidence.values()) < 0.2:
            counters["incoherent_groups"] += 1
            continue
        proposals.append({
            "proposal_type": "new_collection", "target": None,
            "title": Counter(item["title"] for item in items).most_common(1)[0][0], "score": score,
            "evidence": {"match": "title_core", "normalized_value": core, **evidence},
            "members": [item["snapshot"] for item in items],
        })
        counters["new_collection_proposals"] += 1
        counters["new_collection_publications"] += len(items)
        progress({"phase": "grouping", **dict(counters)})
    return proposals, dict(counters)
