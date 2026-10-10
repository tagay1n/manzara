"""Discover deterministic collection proposals through the inline operations CLI."""

from app.catalog.collection_discovery import COLLECTION_DISCOVERY_CONTRACT, CollectionDiscoveryStore
from app.catalog.repository import CatalogRepository
from app.modules.library.collection_detection import (
    build_publication_features, collection_signatures, discover_collections,
)
from app.postgres_engine import get_postgres_engine
from app.task_runtime.contracts import RunContext


def execute(context: RunContext) -> dict:
    if context.options.workers != 1:
        raise ValueError("Discover collections requires one worker")
    if (context.options.limit is not None or context.options.per_mime_limit is not None
            or context.options.only_md5s or context.options.retry_known_failures):
        raise ValueError("Discover collections requires the complete inventory; clear candidate/retry options")
    summary = {"kind": "library.collection_discovery_summary", "contract_version": COLLECTION_DISCOVERY_CONTRACT}

    def check_stop():
        if context.should_stop():
            raise InterruptedError("Collection discovery stopped; proposal refresh was not committed")

    try:
        check_stop()
        store = CollectionDiscoveryStore(CatalogRepository(
            get_postgres_engine(context.db.database_url, schema=context.db.schema), schema=context.db.schema,
        ))
        store.check()
        context.log("Collection discovery: reading publication metadata; path evidence disabled; AI disabled")
        context.progress({"phase": "discovering"}, force=True)
        inventory = store.inventory(check_stop)
        features = []
        for row in inventory["publications"]:
            check_stop()
            features.append(build_publication_features(row))
            if len(features) % 1000 == 0:
                context.progress({"phase": "discovering", "scanned": len(features)})
        context.log(f"Collection inventory: publications={len(features)} collections={len(inventory['collections'])}")
        context.progress({"phase": "matching", "scanned": len(features)}, force=True)
        matches = store.match_collections(features, collection_signatures(features, inventory["collections"]), check_stop)
        context.progress({"phase": "grouping", "scanned": len(features)}, force=True)
        candidates, counters = discover_collections(
            features, inventory["collections"], matches, should_stop=check_stop, progress=context.progress,
        )
        context.log(f"Collection candidates: new={counters.get('new_collection_proposals', 0)} "
                    f"attachments={counters.get('attachment_proposals', 0)} excluded={counters.get('excluded', 0)}")
        context.progress({"phase": "persisting proposals", **counters}, force=True)
        result = store.refresh(inventory, candidates, should_stop=check_stop, log=context.log)
        return {**summary, **counters, **result, "outcome": "completed", "stopped": False}
    except InterruptedError:
        context.log("Collection discovery stopped; previous proposals retained")
        return {**summary, "outcome": "stopped", "stopped": True}
