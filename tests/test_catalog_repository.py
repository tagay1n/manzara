"""Durable catalog behavior on the isolated PostgreSQL test backend."""

import uuid

import pytest
from sqlalchemy import create_engine, select, text

from app.catalog.repository import CatalogRepository
from app.catalog.schema import build_metadata
from app.catalog.contracts import CatalogConflict
from app.catalog.importer import import_snapshot


@pytest.fixture
def catalog(test_database_url):
    schema = "catalog_test_" + uuid.uuid4().hex[:10]
    engine = create_engine(test_database_url)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        build_metadata(schema).create_all(conn)
    repository = CatalogRepository(engine, schema=schema)
    yield repository
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


def document(catalog, suffix="a", **fields):
    return catalog.create_document(suffix * 32, mime_type="application/pdf", actor="test", **fields)


def test_snapshot_reads_use_bounded_server_cursors_without_losing_rows(catalog):
    from sqlalchemy import event
    from app.catalog.importer import read_snapshot

    schema = catalog.schema
    with catalog.engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{schema}".document (md5 TEXT PRIMARY KEY)'))
        conn.execute(text(f'INSERT INTO "{schema}".document SELECT md5(i::text) FROM generate_series(1,1201) i'))
    cursors = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement == f'SELECT * FROM "{schema}"."document"':
            cursors.append((cursor.name, context.execution_options.get("yield_per")))

    event.listen(catalog.engine, "before_cursor_execute", observe)
    try:
        source = read_snapshot(catalog.engine, domain_schema=schema, dataset_schema=schema)
    finally:
        event.remove(catalog.engine, "before_cursor_execute", observe)
    assert len(source["document"]) == 1201
    assert len({row["md5"] for row in source["document"]}) == 1201
    assert cursors and all(name and size == 1024 for name, size in cursors)


def test_import_batches_bound_payload_bytes_without_losing_oversized_evidence():
    from app.catalog.importer import _bounded_batches
    import json

    rows = [{"payload": "é" * 40}, {"payload": "short"},
            {"payload": "x" * 800}, {"payload": "last"}]
    batches = list(_bounded_batches(rows, max_rows=2, max_bytes=300))
    assert [row for batch in batches for row in batch] == rows
    assert all(len(batch) <= 2 for batch in batches)
    assert all(sum(len(json.dumps(row, default=str).encode("utf-8")) for row in batch) <= 300
               or len(batch) == 1 for batch in batches)
    assert any(batch == [rows[2]] for batch in batches)


def test_copy_import_evidence_preserves_json_unicode_and_nulls(catalog):
    from app.catalog.importer import _copy_evidence

    row = {"md5": None, "record_kind": "import", "record_key": "source:0", "source": "legacy.source",
           "payload": {"text": 'A quote " and comma,\nnext line é \\N', "values": [None, True, 3]}}
    with catalog.engine.begin() as conn:
        _copy_evidence(conn, catalog.table("evidence"), [row])
    with catalog.engine.connect() as conn:
        stored = conn.execute(select(catalog.table("evidence"))).mappings().one()
    assert {key: stored[key] for key in row} == row


def test_copy_import_evidence_remains_in_the_import_transaction(catalog):
    from app.catalog.importer import _copy_evidence

    with pytest.raises(RuntimeError, match="later import failure"):
        with catalog.engine.begin() as conn:
            _copy_evidence(conn, catalog.table("evidence"), [{"md5": None,
                "record_kind": "import", "record_key": "source:0", "source": "legacy.source", "payload": {}}])
            raise RuntimeError("later import failure")
    with catalog.engine.connect() as conn:
        assert conn.execute(select(catalog.table("evidence"))).first() is None


def test_lean_import_preserves_unresolved_alias_workflow_without_copying_checkpoints(catalog):
    source = {
        "document": [{"md5": "a" * 32, "sharing_restricted": False, "full": True}],
        "normalization_aliases": [{"alias_id": 42, "entity_type": "publisher", "raw_name": "Unresolved Press",
            "normalized_name": "unresolved press", "script_label": "latin", "decision_status": "pending",
            "canonical_id": None, "docs_count": 7, "mentions_count": 9, "marker_count": 0,
            "successful_model": "reviewed-model", "source_roles": ["publisher"], "created_at": "before", "updated_at": "after"}],
        "library_metadata_quality_state": [{"md5": "a" * 32, "status": "resolved"}],
    }
    report = import_snapshot(catalog, source, actor="migration", evidence_mode="essential")
    with catalog.engine.connect() as conn:
        reviews, names = catalog.table("alias_reviews"), catalog.table("names")
        row = conn.execute(select(reviews, names.c.raw_name).join(names)).mappings().one()
        assert row["alias_id"] == 42 and row["raw_name"] == "Unresolved Press"
        assert row["decision_status"] == "pending" and row["entity_id"] is None
        assert row["docs_count"] == 7 and row["successful_model"] == "reviewed-model"
        assert conn.execute(select(catalog.table("evidence"))).first() is None
    assert report["evidence_mode"] == "essential"


def test_lean_import_preserves_retired_fields_and_only_lost_source_values(catalog):
    source = {
        "document": [{"md5": "a" * 32, "sharing_restricted": None, "full": True, "future_source_field": "retained"}],
        "metadata": [{"md5": "a" * 32, "schema_org": {"@type": "Book", "name": "Original"}, "lib": False}],
        "library_collections": [{"collection_id": 3, "title": "Series", "normalized_title": "series",
            "metadata_template_json": '{"genre":["Literature"]}', "applied_at": "original-apply",
            "created_at": "original-created", "updated_at": "original-updated"}],
        "library_collection_items": [{"collection_id": 3, "md5": "a" * 32, "item_title": "Distinct item title",
            "created_at": "joined", "updated_at": "changed"}],
    }
    import_snapshot(catalog, source, actor="migration", evidence_mode="essential")
    with catalog.engine.connect() as conn:
        collection = conn.execute(select(catalog.table("collections"))).mappings().one()
        publication = conn.execute(select(catalog.table("publications"))).mappings().one()
        evidence = conn.execute(select(catalog.table("evidence"))).mappings().all()
    assert collection["metadata_template_json"] == '{"genre":["Literature"]}'
    assert collection["applied_at"] == "original-apply"
    assert publication["collection_item_title"] == "Distinct item title"
    assert publication["collection_created_at"] == "joined"
    assert len(evidence) == 1
    assert evidence[0]["payload"] == {"sharing_restricted": None, "future_source_field": "retained"}


def test_admin_fields_are_protected_and_automated_changes_become_proposals(catalog):
    row = document(catalog)
    pub = catalog.patch("publication", row["publication_id"], {"revision": 1, "name": "Reviewed"}, actor="owner")
    result = catalog.apply_metadata(row["md5"], {"@context": "https://schema.org", "@type": "Book", "name": "AI", "description": "New"}, actor="extractor", automated=True)
    assert result["scalars"]["name"] == "Reviewed"
    assert result["scalars"]["description"] == "New"
    assert catalog.list_proposals(kind="metadata")["items"][0]["status"] == "pending"
    with pytest.raises(CatalogConflict):
        catalog.patch("publication", pub["publication_id"], {"revision": 1, "name": "Stale"}, actor="owner")


def test_homonyms_are_not_globally_reassigned(catalog):
    first, second = document(catalog), document(catalog, "b")
    schema = {"@type": "Book", "author": [{"@type": "Person", "name": "A. Example"}]}
    catalog.apply_metadata(first["md5"], schema, actor="import")
    catalog.apply_metadata(second["md5"], schema, actor="import")
    people = [catalog.create_entity("person", name, actor="owner") for name in ["Alice Example", "Alex Example"]]
    credits = [catalog.metadata(row["md5"])["credits"][0] for row in [first, second]]
    for credit, person in zip(credits, people):
        catalog.resolve_contribution(credit["contribution_id"], person["entity_id"], revision=credit["revision"], actor="owner")
    assert len(catalog.list_aliases(people[0]["entity_id"])) == 1
    assert len(catalog.list_aliases(people[1]["entity_id"])) == 1
    assert catalog.metadata(first["md5"])["credits"][0]["entity_id"] != catalog.metadata(second["md5"])["credits"][0]["entity_id"]


def test_publication_grouping_requires_explicit_conflict_resolutions(catalog):
    first, second = document(catalog), document(catalog, "b")
    catalog.patch("publication", first["publication_id"], {"revision": 1, "name": "A"}, actor="owner")
    catalog.patch("publication", second["publication_id"], {"revision": 1, "name": "B"}, actor="owner")
    with pytest.raises(ValueError, match="conflict"):
        catalog.merge_publications(first["publication_id"], {second["publication_id"]: 2}, revision=2, resolutions={}, actor="owner")
    catalog.merge_publications(first["publication_id"], {second["publication_id"]: 2}, revision=2, resolutions={"name": "A"}, actor="owner")
    assert catalog.get("document", second["md5"])["publication_id"] == first["publication_id"]


def test_private_preview_requests_survive_retries_and_keep_the_previous_generation(catalog):
    row = document(catalog, restricted=True)
    request = catalog.request_preview(row["md5"], actor="owner", idempotency_key="first")
    assert catalog.request_preview(row["md5"], actor="owner", idempotency_key="first") == request
    claimed = catalog.claim_preview("worker")
    assert claimed["private"] is True
    renewed = catalog.renew_preview(claimed["request_id"], claimed["claim_token"], lease_seconds=3600)
    assert renewed["lease_until"] > claimed["lease_until"]
    with pytest.raises(CatalogConflict):
        catalog.renew_preview(claimed["request_id"], "replaced-token")
    catalog.finish_preview(claimed["request_id"], claimed["claim_token"], pages=[{"role": "first", "page_number": 1,
        "small_key": "private/a.webp", "large_key": "private/b.webp"}], source_page_count=1, actor="worker")
    next_request = catalog.request_preview(row["md5"], actor="owner", idempotency_key="second")
    assert next_request["request_id"] != request["request_id"]
    assert catalog.preview(row["md5"])["request_id"] == request["request_id"]


def test_inclusion_search_covers_excluded_and_missing_metadata(catalog):
    first, second = document(catalog), document(catalog, "b")
    catalog.patch("publication", first["publication_id"], {"revision": 1, "inclusion": "excluded"}, actor="owner")
    assert catalog.list_documents(inclusion="all")["total"] == 2
    assert catalog.list_documents(inclusion="excluded")["items"][0]["md5"] == first["md5"]
    assert catalog.list_documents(filters=[{"field": "md5", "pattern": "^b"}])["items"][0]["md5"] == second["md5"]
    with pytest.raises(ValueError, match="regular expression"):
        catalog.list_documents(filters=[{"field": "title", "pattern": "["}])


def test_taxonomy_branch_edit_updates_paths_and_rejects_cycles(catalog):
    root = catalog.create_classification_node("800", "Literature", actor="owner")
    child = catalog.create_classification_node("800", "Poetry", parent_id=root["node_id"], actor="owner")
    catalog.patch("classification_node", root["node_id"], {"revision": 1, "label_en": "Arts"}, actor="owner")
    assert catalog.classification_path(child["node_id"])["path_en"] == ["Arts", "Poetry"]
    with pytest.raises(ValueError, match="cycle"):
        catalog.patch("classification_node", root["node_id"], {"revision": 2, "parent_id": child["node_id"]}, actor="owner")


def test_taxonomy_edit_invalidates_publication_metadata_revision(catalog):
    row = document(catalog)
    node = catalog.create_classification_node("800", "Literature", actor="owner")
    catalog.assign_classification(row["publication_id"], node["node_id"], revision=1, actor="owner")
    revision = catalog.get("publication", row["publication_id"])["revision"]
    catalog.patch("classification_node", node["node_id"], {"revision": 1, "label_en": "Arts"}, actor="owner")
    assert catalog.get("publication", row["publication_id"])["revision"] > revision


def test_import_preserves_evidence_and_does_not_infer_approval(catalog):
    from app.catalog.importer import import_snapshot, validate_snapshot

    source = {
        "document": [{"md5": "a" * 32, "mime_type": "application/pdf", "full": True,
                      "sharing_restricted": False, "language": "tt-Cyrl", "ya_path": "/source/a.pdf"}],
        "metadata": [{"md5": "a" * 32, "lib": False,
                      "schema_org": {"@context": "https://schema.org", "@type": "Book", "name": "A", "inLanguage": "en"}}],
        "normalization_canonicals": [{"canonical_id": 40, "entity_type": "personality", "display_name": "A Person", "status": "active"}],
        "library_upstream_metadata": [{"md5": "b" * 32, "payload_json": {"title": "Retained"}}],
    }
    assert validate_snapshot(source)["language_mismatches"] == ["a" * 32]
    manifest = import_snapshot(catalog, source, actor="migration", allow_language_mismatches=True)
    assert manifest["documents"] == 1
    assert catalog.list_documents(inclusion="excluded")["total"] == 1
    assert catalog.get("entity", 40)["approval"] == "unconfirmed"
    assert catalog.schema_org("a" * 32)["inLanguage"] == "en"
    with catalog.engine.connect() as conn:
        evidence = conn.execute(select(catalog.table("evidence"))).mappings().all()
    assert any(row["md5"] == "b" * 32 and row["source"] == "legacy.library_upstream_metadata" for row in evidence)


def test_cluster_merge_detects_stale_members_and_preserves_roles(catalog):
    first = catalog.create_entity("person", "A", actor="owner")
    second = catalog.create_entity("person", "B", actor="owner")
    proposal = catalog.create_cluster([{"entity_id": first["entity_id"]}, {"entity_id": second["entity_id"]}], display_name="A", evidence={}, actor="analysis")
    catalog.patch("entity", first["entity_id"], {"revision": 1, "notes": "Reviewed"}, actor="owner")
    with pytest.raises(CatalogConflict):
        catalog.decide_cluster(proposal["proposal_id"], revision=1, decision="merge", actor="owner")


def test_metadata_proposal_requires_reviewed_current_publication(catalog):
    row = document(catalog)
    catalog.patch("publication", row["publication_id"], {"revision": 1, "name": "Reviewed"}, actor="owner")
    catalog.apply_metadata(row["md5"], {"@type": "Book", "name": "Suggested"}, actor="AI", automated=True)
    proposal = catalog.list_proposals(kind="metadata")["items"][0]
    details = catalog.proposal_detail(proposal["proposal_id"])
    assert details["current_publication"]["revision"] == 3
    with pytest.raises(CatalogConflict):
        catalog.decide_metadata(proposal["proposal_id"], revision=1, publication_revision=2, decision="apply", actor="owner")
    catalog.decide_metadata(proposal["proposal_id"], revision=1, publication_revision=3, decision="apply", actor="owner")
    assert catalog.schema_org(row["md5"])["name"] == "Suggested"


def test_removing_alias_retains_contextually_resolved_mentions(catalog):
    row = document(catalog)
    catalog.apply_metadata(row["md5"], {"@type": "Book", "author": [{"@type": "Person", "name": "Alias"}]}, actor="import")
    person = catalog.create_entity("person", "Canonical", actor="owner")
    credit = catalog.metadata(row["md5"])["credits"][0]
    catalog.resolve_contribution(credit["contribution_id"], person["entity_id"], revision=1, actor="owner")
    alias = catalog.list_aliases(person["entity_id"])[0]
    catalog.remove_alias(alias["alias_id"], revision=alias["revision"], actor="owner")
    assert catalog.list_aliases(person["entity_id"]) == []
    assert catalog.metadata(row["md5"])["credits"][0]["entity_id"] == person["entity_id"]


def test_canonical_rename_changes_projection_but_preserves_source_name(catalog):
    row = document(catalog)
    catalog.apply_metadata(row["md5"], {"@type": "Book", "publisher": {"@type": "Organization", "name": "Raw"}}, actor="import")
    entity = catalog.create_entity("organization", "Canonical", actor="owner")
    credit = catalog.metadata(row["md5"])["credits"][0]
    catalog.resolve_contribution(credit["contribution_id"], entity["entity_id"], revision=1, actor="owner")
    catalog.patch("entity", entity["entity_id"], {"revision": 2, "display_name": "New"}, actor="owner")
    assert catalog.schema_org(row["md5"])["publisher"]["name"] == "New"
    assert catalog.metadata(row["md5"])["credits"][0]["raw_name"] == "Raw"


def test_snapshot_fingerprint_is_independent_of_row_order():
    from app.catalog.importer import snapshot_fingerprint

    first = {"document": [{"md5": "b" * 32}, {"md5": "a" * 32}]}
    second = {"document": list(reversed(first["document"]))}
    assert snapshot_fingerprint(first) == snapshot_fingerprint(second)


def test_unchanged_credit_does_not_create_proposal_or_lose_resolution(catalog):
    row = document(catalog)
    source = {"@type": "Book", "author": [{"@type": "Person", "name": "Raw"}]}
    catalog.apply_metadata(row["md5"], source, actor="import", automated=True)
    entity = catalog.create_entity("person", "Canonical", actor="owner")
    credit = catalog.metadata(row["md5"])["credits"][0]
    catalog.resolve_contribution(credit["contribution_id"], entity["entity_id"], revision=1, actor="owner")
    catalog.apply_metadata(row["md5"], source, actor="AI", automated=True)
    assert catalog.list_proposals()["total"] == 0
    assert catalog.metadata(row["md5"])["credits"][0]["contribution_id"] == credit["contribution_id"]


def test_classification_projection_uses_edited_tree(catalog):
    row = document(catalog)
    node = catalog.create_classification_node("800", "Literature", actor="owner")
    catalog.assign_classification(row["publication_id"], node["node_id"], revision=1, actor="owner")
    catalog.patch("classification_node", node["node_id"], {"revision": 1, "label_en": "Arts"}, actor="owner")
    terms = catalog.schema_org(row["md5"])["about"]
    assert next(term for term in terms if term["inDefinedTermSet"]["name"] == "CategoryPath")["termCode"] == "Arts"


def test_import_preserves_actionable_identity_suggestions_and_separations(catalog):
    from app.catalog.importer import import_snapshot

    source = {
        "document": [{"md5": "a" * 32, "full": True}],
        "normalization_canonicals": [{"canonical_id": 10, "entity_type": "personality", "display_name": "Person", "status": "active"}],
        "normalization_suggestions": [{"suggestion_id": 7, "entity_type": "personality", "raw_name": "P.",
            "normalized_name": "Person", "target_canonical_id": 10, "status": "open"}],
        "publisher_merge_proposals": [{"proposal_id": 5, "status": "staged",
            "proposal": {"proposed_name": "Publisher", "member_ids": ["raw:Pub", "raw:Publisher"]},
            "members": [], "review_edit": None}],
        "publisher_separations": [{"left_key": "raw:Pub", "right_key": "raw:Publisher"}],
    }
    import_snapshot(catalog, source, actor="migration")
    proposals = catalog.list_proposals()["items"]
    assert len(proposals) == 2
    publisher = next(row for row in proposals if row["display_name"] == "Publisher")
    details = catalog.proposal_detail(publisher["proposal_id"])
    assert len(details["members"]) == 2 and len(details["separations"]) == 1
    assert publisher["evidence"]["legacy_status"] == "staged"


@pytest.mark.parametrize("publisher_kind,author_kind", [("Organization", "Person"), ("Person", "Organization")])
def test_import_publisher_reviews_use_publisher_mentions_for_homonym_kind(catalog, publisher_kind, author_kind):
    source = {
        "document": [{"md5": "a" * 32}],
        "metadata": [{"md5": "a" * 32, "schema_org": {"@type": "Book",
            "publisher": {"@type": publisher_kind, "name": "Shared"},
            "author": {"@type": author_kind, "name": "Shared"}}}],
        "publisher_merge_proposals": [{"proposal_id": 1, "status": "pending",
            "proposal": {"proposed_name": "Candidate", "member_ids": ["raw:Shared", "raw:Other"]}}],
        "publisher_separations": [{"left_key": "raw:Shared", "right_key": "raw:Other"}],
    }
    import_snapshot(catalog, source, actor="migration")
    proposal = catalog.list_proposals(kind="identity")["items"][0]
    detail = catalog.proposal_detail(proposal["proposal_id"])
    shared = next(member for member in detail["members"] if member["snapshot"].get("raw_name") == "Shared")
    assert shared["snapshot"]["kind"] == publisher_kind.lower()
    assert len(detail["separations"]) == 1


def test_restricted_document_cannot_serve_an_old_public_generation(catalog):
    row = document(catalog)
    catalog.request_preview(row["md5"], actor="owner", idempotency_key="public")
    claimed = catalog.claim_preview("worker")
    catalog.finish_preview(claimed["request_id"], claimed["claim_token"], pages=[{"role": "first", "page_number": 1,
        "small_key": "public/a.webp", "large_key": "public/b.webp"}], source_page_count=1, actor="worker")
    catalog.patch("document", row["md5"], {"revision": 1, "restricted": True}, actor="owner")
    assert catalog.preview(row["md5"]) is None


def test_automated_accessibility_changes_to_protected_files_become_proposals(catalog):
    row = document(catalog)
    catalog.apply_metadata(row["md5"], {"@type": "Book", "accessMode": ["textual"]}, actor="owner", revision=1)
    catalog.apply_metadata(row["md5"], {"@type": "Book", "accessMode": ["visual"]}, actor="AI", automated=True)
    proposal = catalog.list_proposals(kind="metadata")["items"][0]
    assert proposal["field_changes"]["access_modes"] == ["visual"]
    assert catalog.schema_org(row["md5"])["accessMode"] == ["textual"]


def test_metadata_editor_preserves_unchanged_resolved_credits(catalog):
    row = document(catalog)
    source = {"@type": "Book", "author": [{"@type": "Person", "name": "Raw"}]}
    catalog.apply_metadata(row["md5"], source, actor="import", automated=True)
    person = catalog.create_entity("person", "Canonical", actor="owner")
    credit = catalog.metadata(row["md5"])["credits"][0]
    catalog.resolve_contribution(credit["contribution_id"], person["entity_id"], revision=1, actor="owner")
    edited = catalog.schema_org(row["md5"])
    edited["editor"] = [{"@type": "Person", "name": "New editor"}]
    catalog.apply_metadata(row["md5"], edited, actor="owner", revision=3, document_revision=1)
    author = next(item for item in catalog.metadata(row["md5"])["credits"] if item["role"] == "author")
    assert author["contribution_id"] == credit["contribution_id"] and author["entity_id"] == person["entity_id"]


def test_import_follows_merged_identity_target(catalog):
    digest = 'a' * 32
    source = {
        'document': [{'md5': digest}],
        'metadata': [{'md5': digest, 'schema_org': {'@type': 'Book', 'publisher': {'@type': 'Organization', 'name': 'Raw'}}}],
        'normalization_canonicals': [
            {'canonical_id': 1, 'entity_type': 'publisher', 'display_name': 'Old', 'status': 'merged', 'merged_into_id': 2},
            {'canonical_id': 2, 'entity_type': 'publisher', 'display_name': 'Survivor', 'status': 'active'},
        ],
        'normalization_aliases': [{'canonical_id': 1, 'raw_name': 'Raw', 'decision_status': 'linked'}],
    }
    import_snapshot(catalog, source, actor='audit')
    assert catalog.metadata(digest)['credits'][0]['entity_id'] == 2
    assert catalog.schema_org(digest)['publisher']['name'] == 'Survivor'


def test_import_does_not_make_unknown_file_privacy_public(catalog):
    source = {"document": [{"md5": "a" * 32, "sharing_restricted": None, "full": True}]}
    import_snapshot(catalog, source, actor="migration")
    assert catalog.get("document", "a" * 32)["restricted"] is True


def test_reviewed_audience_proposal_preserves_every_audience(catalog):
    row = document(catalog)
    catalog.apply_metadata(row['md5'], {'@type': 'Book', 'audience': {'@type': 'Audience', 'audienceType': 'Adults'}}, actor='owner', revision=1)
    expected = [{'@type': 'Audience', 'audienceType': 'Students'}, {'@type': 'Audience', 'audienceType': 'Children'}]
    catalog.apply_metadata(row['md5'], {'@type': 'Book', 'audience': expected}, actor='AI', automated=True)
    proposal = catalog.list_proposals(kind='metadata')['items'][0]
    revision = catalog.get('publication', row['publication_id'])['revision']
    catalog.decide_metadata(proposal['proposal_id'], revision=1, publication_revision=revision, decision='apply', actor='owner')
    assert catalog.schema_org(row['md5'])['audience'] == expected


def test_identity_merge_invalidates_reviewed_publication_revision(catalog):
    row = document(catalog)
    catalog.apply_metadata(row['md5'], {'@type': 'Book', 'author': [{'@type': 'Person', 'name': 'Raw'}]}, actor='AI', automated=True)
    first = catalog.create_entity('person', 'First', actor='owner')
    second = catalog.create_entity('person', 'Second', actor='owner')
    credit = catalog.metadata(row['md5'])['credits'][0]
    catalog.resolve_contribution(credit['contribution_id'], second['entity_id'], revision=1, actor='owner')
    reviewed = catalog.get('publication', row['publication_id'])['revision']
    cluster = catalog.create_cluster([{'entity_id': first['entity_id']}, {'entity_id': second['entity_id']}], display_name='First', evidence={}, actor='analysis')
    catalog.decide_cluster(cluster['proposal_id'], revision=1, decision='merge', actor='owner')
    assert catalog.get('publication', row['publication_id'])['revision'] > reviewed


def test_import_fills_shared_branch_translation_from_later_path(catalog):
    source = {'document': [], 'classification': [
        {'id': 1, 'ddc': '800', 'path_en': ['Literature', 'Poetry'], 'path_tt': []},
        {'id': 2, 'ddc': '800', 'path_en': ['Literature', 'Prose'], 'path_tt': ['Translated root', 'Translated prose']},
    ]}
    import_snapshot(catalog, source, actor='audit')
    nodes = catalog.table('classification_nodes')
    with catalog.engine.connect() as conn:
        root = conn.execute(select(nodes).where(nodes.c.parent_id.is_(None))).mappings().one()
    assert root['label_tt'] == 'Translated root'

def test_lean_import_preserves_file_language_and_storage_without_metadata_or_locator(catalog):
    digest='a'*32
    source={'document':[{'md5':digest,'language':'tt,en','sharing_restricted':False,'full':True,
        'primary_storage_size':123,'primary_storage_etag':'known'}]}
    import_snapshot(catalog,source,actor='migration',evidence_mode='essential')
    assert catalog.metadata(digest)['languages']==['tt','en']
    with catalog.engine.connect() as conn:
        location=conn.execute(select(catalog.table('locations'))).mappings().one()
        assert location['purpose']=='primary' and location['size']==123 and location['etag']=='known'
        assert conn.execute(select(catalog.table('evidence'))).first() is None

def test_streamed_domain_copy_preserves_arrays_text_dates_and_defaults(catalog):
    from datetime import datetime,timezone
    from app.catalog.importer import _copy_rows
    with catalog.engine.begin() as conn:
        _copy_rows(conn,catalog.table('publications'),[{'publication_id':1,'name':'Text\nquote " \\N é','languages':['tt','a,b','quote "','slash \\'], 'page_count':None,'has_metadata':False}])
        _copy_rows(conn,catalog.table('documents'),[{'md5':'a'*32,'publication_id':1,'restricted':True,'complete':False}])
        moment=datetime(2026,10,6,tzinfo=timezone.utc)
        _copy_rows(conn,catalog.table('locations'),[{'md5':'a'*32,'provider':'s3','purpose':'primary','verified_at':moment,'size':123}])
    with catalog.engine.connect() as conn:
        publication=conn.execute(select(catalog.table('publications'))).mappings().one()
        assert publication['name']=='Text\nquote " \\N é'
        assert publication['languages']==['tt','a,b','quote "','slash \\']
        assert publication['revision']==1 and publication['page_count'] is None and publication['has_metadata'] is False
        assert conn.execute(select(catalog.table('locations').c.verified_at)).scalar_one()==moment


def test_identity_review_import_batches_repeated_members(catalog):
    from sqlalchemy import event
    from app.catalog.import_reviews import import_identity_reviews
    entity=catalog.create_entity('organization','Press',actor='test')
    source={'publisher_merge_proposals':[{'proposal_id':str(i),'status':'pending','proposal':{'member_ids':[f"canonical:{entity['entity_id']}",'raw:Observed'],'proposed_name':'Press'}} for i in range(100)]}
    calls=[]
    def observe(*args):calls.append(args[2])
    event.listen(catalog.engine,'before_cursor_execute',observe)
    try:
        with catalog.engine.begin() as conn:
            import_identity_reviews(catalog,conn,source,actor='migration')
    finally:
        event.remove(catalog.engine,'before_cursor_execute',observe)
    assert len(calls)<25
    assert catalog.list_proposals()['total']==100
    with catalog.engine.connect() as conn:
        assert conn.execute(select(text('count(*)')).select_from(catalog.table('proposal_members'))).scalar_one()==200
