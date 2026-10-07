"""Exercise existing task SQL and admin mutations across the coordinated cutover."""

from pathlib import Path
import uuid
import json

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text

from app.catalog.cutover import activate_catalog
from app.catalog.importer import import_snapshot, read_snapshot
from app.catalog.repository import CatalogRepository
from app.modules.maintenance.monocorpus_sync_repository import MonocorpusSyncRepository
from app.db import Database


@pytest.fixture
def staged_catalog(test_database_url, request):
    schema = "cutover_test_" + uuid.uuid4().hex[:10]
    engine = create_engine(test_database_url, connect_args={"options": f"-csearch_path={schema},public"})
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.exec_driver_sql('''CREATE TABLE document (
            md5 TEXT PRIMARY KEY, mime_type TEXT, ya_path TEXT, ya_public_url TEXT,
            ya_public_key TEXT, ya_resource_id TEXT, language TEXT,
            content_extraction_method TEXT, meta_extraction_method TEXT,
            "full" BOOLEAN, sharing_restricted BOOLEAN, document_url TEXT,
            content_url TEXT, primary_storage_size BIGINT, primary_storage_etag TEXT,
            primary_storage_verified_at TIMESTAMPTZ);
            CREATE TABLE classification (id SERIAL PRIMARY KEY, ddc TEXT NOT NULL,
            path_en JSON NOT NULL, path_en_key TEXT NOT NULL, path_tt JSON,
            status TEXT NOT NULL DEFAULT 'pending', created_by TEXT NOT NULL DEFAULT 'gemini',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ddc,path_en_key));
            CREATE TABLE metadata (md5 TEXT PRIMARY KEY REFERENCES document(md5) ON DELETE CASCADE,
            schema_org JSONB, lib BOOLEAN, lib_eval_method TEXT,
            classification_id INTEGER REFERENCES classification(id) ON DELETE SET NULL)''')
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    for key, value in {"manzara_database_url": test_database_url, "manzara_db_schema": schema,
                       "manzara_alembic_version_schema": schema}.items():
        config.set_main_option(key, value)
    command.upgrade(config, "head")
    catalog = CatalogRepository(engine, schema=schema)
    source = read_snapshot(engine, domain_schema=schema, dataset_schema=schema)
    callspec = getattr(request.node, "callspec", None)
    mode = getattr(request, "param", callspec.params.get("integrated_catalog", "full") if callspec else "full")
    import_snapshot(catalog, source, actor="migration", evidence_mode=mode)
    yield catalog, test_database_url
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture(params=["full", "essential"])
def integrated_catalog(staged_catalog, request):
    catalog, _ = staged_catalog
    with catalog.engine.connect() as conn:
        fingerprint = conn.execute(text("SELECT source_fingerprint FROM catalog_imports")).scalar_one()
    activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema,
                     retire_legacy=request.param == "essential")
    return staged_catalog


@pytest.mark.parametrize("staged_catalog", ["essential"], indirect=True)
def test_lean_retirement_removes_domain_storage_and_preserves_worker_commands(staged_catalog):
    catalog, url = staged_catalog
    with catalog.engine.connect() as conn:
        fingerprint = conn.execute(text("SELECT source_fingerprint FROM catalog_imports")).scalar_one()
    activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema, retire_legacy=True)
    with catalog.engine.connect() as conn:
        for name in ("document", "metadata", "classification", "normalization_canonicals", "normalization_aliases",
                     "library_collections", "library_collection_items"):
            assert conn.execute(text("SELECT relkind FROM pg_class WHERE oid=to_regclass(:name)"),
                                {"name": f'"{catalog.schema}"."{name}"'}).scalar_one() == "v"
        assert conn.execute(text("SELECT manifest->>'legacy_retired' FROM catalog_imports")).scalar_one() == "true"
    sync = MonocorpusSyncRepository(url, schema=catalog.schema)
    try:
        sync.save_discovered_document({"md5": "b" * 32, "mime_type": "application/pdf", "ya_path": "/book.pdf",
            "ya_resource_id": "source", "ya_public_url": None, "ya_public_key": None,
            "full": True, "sharing_restricted": False}, reset_primary_storage=False)
        assert "b" * 32 in sync.list_documents()
        assert catalog.get("document", "b" * 32)["complete"] is True
        with catalog.engine.begin() as conn:
            conn.execute(text("SELECT catalog_upsert('metadata',CAST(:payload AS JSONB),ARRAY['md5'],ARRAY['schema_org'])"),
                         {"payload": json.dumps({"md5": "b" * 32, "schema_org": {"@context": "https://schema.org", "@type": "Book", "name": "Lean"}})})
        assert catalog.schema_org("b" * 32)["name"] == "Lean"
        sync.delete_document_state("b" * 32)
        assert "b" * 32 not in sync.list_documents()
    finally:
        sync.dispose()


def test_cutover_rejects_changed_source_and_leaves_adapters_dormant(staged_catalog):
    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        fingerprint = conn.execute(text("SELECT source_fingerprint FROM catalog_imports")).scalar_one()
        conn.execute(text("INSERT INTO document(md5) VALUES (:md5)"), {"md5": "9" * 32})
    with pytest.raises(ValueError, match="source changed"):
        activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema)
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM catalog_imports")).scalar_one() == "verified"
        assert conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname='catalog_task_document' AND tgrelid='document'::regclass")).scalar_one() == 0


def test_cutover_rejects_new_entities_created_in_staging(staged_catalog):
    catalog, _ = staged_catalog
    with catalog.engine.connect() as conn:
        fingerprint = conn.execute(text("SELECT source_fingerprint FROM catalog_imports")).scalar_one()
    catalog.create_entity("person", "Unexpected staging edit", actor="owner")
    with pytest.raises(ValueError, match="staging catalog was edited"):
        activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema)


def test_task_sync_and_metadata_writes_reach_catalog_without_overwriting_admin(integrated_catalog):
    catalog, url = integrated_catalog
    sync = MonocorpusSyncRepository(url, schema=catalog.schema)
    digest = "a" * 32
    try:
        sync.save_discovered_document({"md5": digest, "mime_type": "application/pdf",
            "ya_path": "/documents/book.pdf", "ya_resource_id": "source-1", "ya_public_url": None,
            "ya_public_key": None, "full": True, "sharing_restricted": False}, reset_primary_storage=False)
        doc = catalog.get("document", digest)
        catalog.patch("publication", doc["publication_id"], {"revision": 1, "name": "Reviewed"}, actor="owner")
        with catalog.engine.begin() as conn:
            conn.execute(text("""SELECT catalog_upsert('metadata', jsonb_build_object('md5', :md5, 'schema_org', CAST(:source AS JSONB), 'lib', NULL, 'classification_id', NULL), ARRAY['md5']::text[], ARRAY['schema_org','lib','classification_id']::text[], ARRAY[]::text[])"""),
                {"md5": digest, "source": '{"@type":"Book","name":"Automated","inLanguage":"en","author":[{"@type":"Person","name":"Raw"}]}'})
            conn.execute(text("UPDATE document SET meta_extraction_method='model/prompt',language='en' WHERE md5=:md5"), {"md5": digest})
        assert catalog.schema_org(digest)["name"] == "Reviewed"
        assert catalog.schema_org(digest)["author"][0]["name"] == "Raw"
        assert catalog.list_proposals(kind="metadata")["total"] == 1
        assert catalog.get("document", digest)["meta_extraction_method"] == "model/prompt"
        with catalog.engine.connect() as conn:
            assert conn.execute(text("SELECT schema_org->>'name' FROM metadata WHERE md5=:md5"), {"md5": digest}).scalar_one() == "Reviewed"
    finally:
        sync.dispose()


def test_admin_taxonomy_collections_and_contextual_names_reach_task_reads(integrated_catalog):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("b" * 32, mime_type="application/pdf", actor="test")
    catalog.apply_metadata(doc["md5"], {"@type": "Book", "name": "Title",
        "author": [{"@type": "Person", "name": "Raw"}]}, actor="AI", automated=True)
    person = catalog.create_entity("person", "Canonical", actor="owner")
    mention = catalog.metadata(doc["md5"])["credits"][0]
    catalog.resolve_contribution(mention["contribution_id"], person["entity_id"], revision=1, actor="owner")
    node = catalog.create_classification_node("800", "Literature", actor="owner")
    revision = catalog.get("publication", doc["publication_id"])["revision"]
    catalog.assign_classification(doc["publication_id"], node["node_id"], revision=revision, actor="owner")
    collection = catalog.create_collection("Collected", actor="owner")
    pub = catalog.get("publication", doc["publication_id"])
    catalog.patch("publication", pub["publication_id"], {"revision": pub["revision"],
        "collection_id": collection["collection_id"], "inclusion": "included"}, actor="owner")
    with catalog.engine.connect() as conn:
        row = conn.execute(text("SELECT m.*,c.path_en,i.collection_id FROM metadata m "
            "JOIN classification c ON c.id=m.classification_id JOIN library_collection_items i ON i.md5=m.md5" )).mappings().one()
    assert row["schema_org"]["author"][0]["name"] == "Canonical"
    assert row["path_en"] == ["Literature"]
    assert row["collection_id"] == collection["collection_id"]
    assert row["lib"] is True


def test_task_evaluation_and_collection_assignment_update_normalized_publication(integrated_catalog):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("c" * 32, actor="test")
    with catalog.engine.begin() as conn:
        classification = conn.execute(text("INSERT INTO classification(ddc,path_en,path_en_key) "
            "VALUES ('800','[\"Literature\",\"Poetry\"]','literature > poetry') RETURNING id")).scalar_one()
        collection = conn.execute(text("INSERT INTO library_collections(title,normalized_title,created_at,updated_at) "
            "VALUES ('Series','series','now','now') RETURNING collection_id")).scalar_one()
        conn.execute(text("""SELECT catalog_upsert('library_collection_items', jsonb_build_object('collection_id', :collection, 'md5', :md5, 'created_at', 'now', 'updated_at', 'now'), ARRAY['md5']::text[], ARRAY[]::text[], ARRAY[]::text[])"""), {"collection": collection, "md5": doc["md5"]})
        conn.execute(text("UPDATE metadata SET lib=TRUE,lib_eval_method='model/evaluation',classification_id=:classification WHERE md5=:md5"),
                     {"classification": classification, "md5": doc["md5"]})
    pub = catalog.get("publication", doc["publication_id"])
    assert pub["inclusion"] == "included"
    assert pub["classification_id"] == classification
    assert pub["collection_id"] == collection
    assert catalog.schema_org(doc["md5"])["about"][-1]["termCode"] == "Literature > Poetry"


def test_normalization_task_keeps_unconfirmed_hypotheses_and_never_sweeps_mentions(integrated_catalog, tmp_path):
    catalog, url = integrated_catalog
    doc = catalog.create_document("d" * 32, actor="test")
    catalog.apply_metadata(doc["md5"], {"@type": "Book", "author": [{"@type": "Person", "name": "Raw"}]}, actor="AI", automated=True)
    db = Database(url, schema=catalog.schema, local_state_path=tmp_path / "runtime.sqlite3")
    try:
        entity = db.create_normalization_canonical("personality", "Canonical", "canonical")
        assert catalog.get("entity", entity["canonical_id"])["approval"] == "unconfirmed"
        with catalog.engine.begin() as conn:
            conn.execute(text("""SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', 'personality', 'raw_name', 'Raw', 'normalized_name', 'raw', 'script_label', 'other', 'decision_status', 'linked', 'canonical_id', :id, 'created_at', 'now', 'updated_at', 'now'), ARRAY['entity_type','raw_name']::text[], ARRAY['canonical_id']::text[], ARRAY[]::text[])"""), {"id": entity["canonical_id"]})
        assert catalog.metadata(doc["md5"])["credits"][0]["entity_id"] is None
        assert catalog.list_aliases(entity["canonical_id"])[0]["approval"] == "unconfirmed"
        db.rename_normalization_canonical("personality", entity["canonical_id"], "Reviewed canonical", "reviewed canonical")
        assert catalog.get("entity", entity["canonical_id"])["approval"] == "confirmed"
    finally:
        db.close()


@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_personality_runner_reads_and_persists_after_legacy_retirement(integrated_catalog, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.modules.library.personality_normalization import PersonComponents
    from app.modules.library.runtime import run_normalize_personalities as runner

    catalog, url = integrated_catalog
    doc=catalog.create_document('1'*32,actor='test')
    source={'@type':'Book','name':'Test','inLanguage':'en','author':[
        {'@type':'Person','name':name} for name in ('Taylor A','Taylor B','Institution','Damaged')]}
    catalog.apply_metadata(doc['md5'],source,actor='AI',automated=True)
    publication=catalog.get('publication',doc['publication_id'])
    catalog.patch('publication',publication['publication_id'],
                  {'revision':publication['revision'],'inclusion':'included'},actor='test')
    excluded=catalog.create_document('2'*32,actor='test')
    catalog.apply_metadata(excluded['md5'],{'@type':'Book','author':{'@type':'Person','name':'Excluded'}},
                           actor='AI',automated=True)
    requests=[]

    def pool(**kwargs):
        raw=kwargs['request']('test-model','test-key',None)
        return SimpleNamespace(value=kwargs['parse'](raw),model_name='test-model')

    def request(**kwargs):
        name=kwargs['contents'][0].split('<raw_name>')[1].split('</raw_name>')[0]
        requests.append(name)
        outcome={'Institution':'not_person','Damaged':'unusable'}.get(name,'normalized')
        response={'outcome':outcome,'reason':None if outcome=='normalized' else 'Reviewed model decision',
                  **{field:None for field in PersonComponents.model_fields}}
        if outcome=='normalized':
            response.update(surname_full='Taylor',name_full='Alex')
        return json.dumps(response)

    monkeypatch.setattr(runner,'run_ordered_model_pool',pool)
    db=Database(url,schema=catalog.schema,local_state_path=tmp_path/'runtime.sqlite3')
    try:
        assert len(db.list_personality_source_documents())==1
        args={'db':db,'models':['test-model'],'run_id':None,'should_stop':lambda:False,
              'workers':2,'request_json':request}
        summary=runner.run_personality_normalization(**args)
        assert summary['outcome']=='completed'
        assert (summary['succeeded'],summary['not_person'],summary['unusable'])==(2,1,1)
        assert sorted(requests)==['Damaged','Institution','Taylor A','Taylor B']
        successes=[db.get_personality_checkpoint(name) for name in ('Taylor A','Taylor B')]
        assert successes[0]['canonical_id']==successes[1]['canonical_id']
        entity=catalog.get('entity',successes[0]['canonical_id'])
        assert entity['kind']=='person' and entity['approval']=='unconfirmed'
        assert entity['name_full']=='Alex' and entity['surname_full']=='Taylor'
        aliases=catalog.list_aliases(entity['entity_id'])
        assert len(aliases)==2 and all(alias['approval']=='unconfirmed' for alias in aliases)
        assert all(credit['entity_id'] is None for credit in catalog.metadata(doc['md5'])['credits'])
        assert catalog.schema_org(doc['md5'])['author']==source['author']
        assert db.get_personality_checkpoint('Institution')['canonical_id'] is None
        assert db.get_personality_checkpoint('Damaged')['canonical_id'] is None
        requests.clear()
        summary=runner.run_personality_normalization(**args)
        assert summary['skipped']==4 and summary['remaining']==0 and requests==[]
    finally:
        db.close()


@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_personality_source_read_uses_relations_without_full_metadata_reconstruction(integrated_catalog, tmp_path):
    from app.modules.library.personality_normalization import extract_personality_candidates

    catalog,url=integrated_catalog
    doc=catalog.create_document('3'*32,actor='test')
    source={'@type':'Book','name':'Test','inLanguage':'en,tt',
        'author':[{'@type':'Person','name':'Raw'},{'@type':'Organization','name':'Institution'}],
        'editor':{'@type':'Person','name':'Raw'},
        'contributor':[{'@type':'Role','roleName':'Compiler','contributor':
            {'@type':'Person','name':'Nested'}} for _ in range(2)],
        'publisher':{'@type':'Person','name':'Publisher only'}}
    catalog.apply_metadata(doc['md5'],source,actor='AI',automated=True)
    publication=catalog.get('publication',doc['publication_id'])
    catalog.patch('publication',publication['publication_id'],
        {'revision':publication['revision'],'inclusion':'included'},actor='test')
    identity=catalog.create_entity('person','Reviewed',actor='owner')
    credit=next(item for item in catalog.metadata(doc['md5'])['credits'] if item['role']=='author' and item['kind']=='person')
    catalog.resolve_contribution(credit['contribution_id'],identity['entity_id'],revision=1,actor='owner')
    db=Database(url,schema=catalog.schema,local_state_path=tmp_path/'runtime.sqlite3')
    try:
        expected=extract_personality_candidates([{'md5':doc['md5'],'schema_org':source}])
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(f'''CREATE OR REPLACE FUNCTION "{catalog.schema}".catalog_schema_org(digest TEXT)
                RETURNS JSONB LANGUAGE plpgsql AS $test$ BEGIN RAISE EXCEPTION 'full metadata must not be rebuilt'; END $test$''')
        actual=extract_personality_candidates(db.list_personality_source_documents())
        assert actual==expected
        by_name={candidate.raw_name:candidate for candidate in actual}
        assert set(by_name)=={'Nested','Raw'}
        assert by_name['Nested'].mention_count==2 and by_name['Nested'].roles==('contributor',)
        assert by_name['Raw'].roles==('author','editor') and by_name['Raw'].mention_count==2
    finally:
        db.close()


def test_task_projection_matches_generated_complex_metadata(integrated_catalog):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("e" * 32, actor="test")
    source = {"@type": "Book", "name": "Complex", "inLanguage": "en,tt-Cyrl", "datePublished": "1920-03",
        "isbn": ["123", "456"], "genre": ["Poetry"], "author": [{"@type": "Person", "name": "Writer"}],
        "contributor": [{"@type": "Role", "roleName": "Compiler", "contributor": {"@type": "Person", "name": "One"}}],
        "audience": [{"@type": "Audience", "audienceType": "Students"}, {"@type": "PeopleAudience", "suggestedMinAge": 10}],
        "accessMode": ["textual"], "accessModeSufficient": [{"@type": "ItemList", "itemListElement": ["textual"]}],
        "about": [{"@type": "DefinedTerm", "name": "Term", "inDefinedTermSet": {"@type": "DefinedTermSet", "name": "Custom", "url": "https://example.test/terms"}}],
        "isBasedOn": {"@type": "Book", "name": "Original", "inLanguage": "en", "url": ["https://example.test/original"], "author": [{"@type": "Person", "name": "Original author"}]}}
    catalog.apply_metadata(doc["md5"], source, actor="AI", automated=True)
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT schema_org FROM metadata WHERE md5=:md5"), {"md5": doc["md5"]}).scalar_one() == catalog.schema_org(doc["md5"])
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE metadata SET schema_org=CAST(:source AS JSONB) WHERE md5=:md5"), {"source": json.dumps({**source, "name": "From task"}), "md5": doc["md5"]})
    assert catalog.schema_org(doc["md5"]) == {**source, "@context": "https://schema.org", "name": "From task"}


def test_guarded_document_cleanup_removes_normalized_file_and_checkpoints(integrated_catalog):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("f" * 32, actor="test")
    with catalog.engine.begin() as conn:
        conn.execute(text("DELETE FROM document WHERE md5=:md5"), {"md5": doc["md5"]})
    assert catalog.list_documents()["total"] == 0


def test_new_task_identity_suggestions_are_actionable_in_admin_queue(integrated_catalog):
    catalog, _ = integrated_catalog
    entity = catalog.create_entity("person", "Candidate", actor="AI")
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO normalization_suggestions(entity_type,raw_name,normalized_name,target_canonical_id,"
            "suggestion_kind,confidence,confidence_band,status,created_at,updated_at) "
            "VALUES ('personality','Observed','Candidate',:id,'link',0.9,'high','open','now','now')"), {"id": entity["entity_id"]})
    proposal = catalog.list_proposals(kind="identity")["items"][0]
    assert proposal["display_name"] == "Candidate"
    assert len(catalog.proposal_detail(proposal["proposal_id"])["members"]) == 2
    catalog.decide_cluster(proposal["proposal_id"], revision=1, decision="merge", actor="owner")
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM normalization_suggestions WHERE raw_name='Observed'")).scalar_one() == "accepted"


def test_catalog_export_loads_contextual_credits_and_new_preview_keys(integrated_catalog):
    from app.modules.library.site_export_repository import LibrarySiteExportRepository

    catalog, url = integrated_catalog
    doc = catalog.create_document("1" * 32, mime_type="application/pdf", actor="test")
    catalog.apply_metadata(doc["md5"], {"@type": "Book", "name": "Title", "inLanguage": "en",
        "author": [{"@type": "Person", "name": "Raw"}]}, actor="AI", automated=True)
    entity = catalog.create_entity("person", "Reviewed", actor="owner")
    mention = catalog.metadata(doc["md5"])["credits"][0]
    catalog.resolve_contribution(mention["contribution_id"], entity["entity_id"], revision=1, actor="owner")
    catalog.patch("entity", entity["entity_id"], {"revision": 2, "approval": "confirmed"}, actor="owner")
    pub = catalog.get("publication", doc["publication_id"])
    catalog.patch("publication", pub["publication_id"], {"revision": pub["revision"], "inclusion": "included"}, actor="owner")
    catalog.request_preview(doc["md5"], actor="owner", idempotency_key="one")
    claim = catalog.claim_preview("worker")
    catalog.finish_preview(claim["request_id"], claim["claim_token"], pages=[{"role": "first", "page_number": 1,
        "small_key": "catalog/request/claim/small.webp", "large_key": "catalog/request/claim/large.webp"}], source_page_count=1, actor="worker")
    exporter = LibrarySiteExportRepository(url, schema=catalog.schema)
    try:
        rows, aliases = exporter.load_snapshot()
    finally:
        exporter.dispose()
    assert rows[0]["catalog_contributions"][0]["entity_id"] == entity["entity_id"]
    assert rows[0]["catalog_preview"]["pages"][0]["small_key"] == "catalog/request/claim/small.webp"
    assert aliases[0]["canonical_id"] == entity["entity_id"]


def test_existing_extraction_and_evaluation_entry_points_use_catalog(integrated_catalog, monkeypatch):
    from unittest.mock import Mock
    from sqlalchemy.orm import Session
    from app.modules.library.metadata_extraction import MetadataExtractionRepository
    from app.modules.library.runtime.metadata import evaluation_persistence
    from app.modules.library.runtime.metadata.evaluation_types import Evaluation

    catalog, url = integrated_catalog
    doc = catalog.create_document("2" * 32, mime_type="application/pdf", actor="test")
    extraction = MetadataExtractionRepository(url, schema=catalog.schema, checkpoint_store=Mock())
    try:
        assert extraction.save_success(doc["md5"], schema_org={"@context": "https://schema.org", "@type": "Book",
            "name": "Extracted", "inLanguage": "en", "author": [{"@type": "Person", "name": "Writer"}]}, model_name="test-model")
    finally:
        extraction.dispose()
    monkeypatch.setattr(evaluation_persistence, "get_session", lambda: Session(catalog.engine))
    monkeypatch.setattr(evaluation_persistence, "clear_evaluation_state", lambda _: None)
    evaluation = Evaluation(applicable=True, reason="A book", library_ddc="800", library_path=["Literature", "Poetry"])
    for _ in range(2):
        evaluation_persistence.save_evaluation_result(doc["md5"], evaluation, model_name="test-model", dry_run=False, log=lambda _: None)
    pub = catalog.get("publication", doc["publication_id"])
    assert pub["name"] == "Extracted"
    assert pub["inclusion"] == "included"
    assert catalog.schema_org(doc["md5"])["about"][-1]["termCode"] == "Literature > Poetry"
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM catalog_classifications")).scalar_one() == 1


def test_cutover_is_idempotent_and_cannot_activate_unreviewed_snapshot(integrated_catalog):
    catalog, _ = integrated_catalog
    with catalog.engine.connect() as conn:
        fingerprint = conn.execute(text("SELECT source_fingerprint FROM catalog_imports WHERE state='active'")).scalar_one()
    activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema)
    with pytest.raises(ValueError, match="not been imported"):
        activate_catalog(catalog, reviewed_fingerprint="unreviewed", dataset_schema=catalog.schema)


def test_task_alias_homonyms_and_protected_inclusion_remain_reviewable(integrated_catalog):
    catalog, _ = integrated_catalog
    first = catalog.create_entity("person", "First", actor="AI")
    second = catalog.create_entity("person", "Second", actor="AI")
    doc = catalog.create_document("3" * 32, actor="test")
    catalog.patch("publication", doc["publication_id"], {"revision": 1, "inclusion": "excluded"}, actor="owner")
    with catalog.engine.begin() as conn:
        for identity in (first, second):
            conn.execute(text("""SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', 'personality', 'raw_name', 'Shared', 'normalized_name', 'shared', 'script_label', 'other', 'decision_status', 'linked', 'canonical_id', :id, 'created_at', 'now', 'updated_at', 'now'), ARRAY['entity_type','raw_name']::text[], ARRAY['canonical_id','decision_status']::text[], ARRAY[]::text[])"""), {"id": identity["entity_id"]})
        conn.execute(text("UPDATE metadata SET lib=TRUE WHERE md5=:md5"), {"md5": doc["md5"]})
    assert len(catalog.list_aliases(first["entity_id"])) == len(catalog.list_aliases(second["entity_id"])) == 1
    assert catalog.get("publication", doc["publication_id"])["inclusion"] == "excluded"
    proposal = catalog.list_proposals(kind="metadata")["items"][0]
    pub = catalog.get("publication", doc["publication_id"])
    catalog.decide_metadata(proposal["proposal_id"], revision=1, publication_revision=pub["revision"], decision="apply", actor="owner")
    assert catalog.get("publication", doc["publication_id"])["inclusion"] == "included"


def test_task_ready_previews_are_visible_in_catalog_and_restrictions_are_enforced(integrated_catalog):
    from app.modules.library.preview_repository import LibraryPreviewRepository
    from app.modules.library.previews import PreviewPage

    catalog, url = integrated_catalog
    doc = catalog.create_document("4" * 32, mime_type="application/pdf", actor="test")
    previews = LibraryPreviewRepository(url, schema=catalog.schema)
    try:
        previews.start_attempt(doc["md5"], recipe_version="webp-v2", run_id=None)
        previews.checkpoint(doc["md5"], recipe_version="webp-v2", source_page_count=1,
            selected_pages=[PreviewPage(role="first", page_number=1, object_alias="1")], status="ready", run_id=None)
    finally:
        previews.dispose()
    assert catalog.preview(doc["md5"])["pages"][0]["small_key"] == doc["md5"] + "/1s.webp"
    catalog.patch("document", doc["md5"], {"revision": 1, "restricted": True}, actor="owner")
    assert catalog.preview(doc["md5"]) is None


def test_manual_task_rename_invalidates_credited_publication_revision(integrated_catalog, tmp_path):
    catalog, url = integrated_catalog
    doc = catalog.create_document("5" * 32, actor="test")
    catalog.apply_metadata(doc["md5"], {"@type": "Book", "author": [{"@type": "Person", "name": "Raw"}]}, actor="AI", automated=True)
    entity = catalog.create_entity("person", "Before", actor="owner")
    credit = catalog.metadata(doc["md5"])["credits"][0]
    catalog.resolve_contribution(credit["contribution_id"], entity["entity_id"], revision=1, actor="owner")
    revision = catalog.get("publication", doc["publication_id"])["revision"]
    db = Database(url, schema=catalog.schema, local_state_path=tmp_path / "runtime.sqlite3")
    try:
        db.rename_normalization_canonical("personality", entity["entity_id"], "After", "after")
    finally:
        db.close()
    assert catalog.get("publication", doc["publication_id"])["revision"] > revision


def test_admin_separations_reach_publisher_research_checkpoints(integrated_catalog):
    catalog, _ = integrated_catalog
    first = catalog.create_entity("organization", "One", actor="owner")
    second = catalog.create_entity("organization", "Two", actor="owner")
    cluster = catalog.create_cluster([{"entity_id": first["entity_id"]}, {"entity_id": second["entity_id"]}], display_name="Candidate", evidence={}, actor="analysis")
    catalog.decide_cluster(cluster["proposal_id"], revision=1, decision="keep_separate", actor="owner")
    with catalog.engine.connect() as conn:
        row = conn.execute(text("SELECT left_key,right_key FROM publisher_separations")).mappings().one()
    assert set(row.values()) == {f"canonical:{first['entity_id']}", f"canonical:{second['entity_id']}"}


def test_new_publisher_research_proposals_enter_the_shared_review_queue(integrated_catalog):
    catalog, _ = integrated_catalog
    first = catalog.create_entity("organization", "One", actor="AI")
    second = catalog.create_entity("organization", "Two", actor="AI")
    with catalog.engine.begin() as conn:
        analysis = conn.execute(text("INSERT INTO publisher_merge_analyses(fingerprint,scope,inventory,metadata,state,created_at,updated_at) "
            "VALUES ('fingerprint','all','{}','{}','imported','now','now') RETURNING analysis_id")).scalar_one()
        conn.execute(text("INSERT INTO publisher_merge_proposals(analysis_id,fingerprint,proposal,members,status,created_at,updated_at) "
            "VALUES (:id,'proposal-fingerprint',CAST(:proposal AS JSONB),'[]','pending','now','now')"),
            {"id": analysis, "proposal": json.dumps({"proposed_name": "Combined", "member_ids": [f"canonical:{first['entity_id']}", f"canonical:{second['entity_id']}"]})})
    proposal = catalog.list_proposals(kind="identity")["items"][0]
    assert proposal["display_name"] == "Combined"
    assert len(catalog.proposal_detail(proposal["proposal_id"])["members"]) == 2


@pytest.mark.parametrize("author_kind", [None, "Organization"])
def test_publisher_review_retains_known_person_type_for_raw_members(integrated_catalog, author_kind):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("6" * 32, actor="test")
    source = {"@type": "Book", "publisher": {"@type": "Person", "name": "Person publisher"}}
    if author_kind:
        source["author"] = {"@type": author_kind, "name": "Person publisher"}
    catalog.apply_metadata(doc["md5"], source, actor="AI", automated=True)
    person = catalog.create_entity("person", "Publisher identity", actor="AI")
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO catalog_entity_roles(entity_id,role) VALUES (:id,'publisher')"), {"id": person["entity_id"]})
        analysis = conn.execute(text("INSERT INTO publisher_merge_analyses(fingerprint,scope,inventory,metadata,state,created_at,updated_at) "
            "VALUES ('person-fingerprint','all','{}','{}','imported','now','now') RETURNING analysis_id")).scalar_one()
        conn.execute(text("INSERT INTO publisher_merge_proposals(analysis_id,fingerprint,proposal,members,status,created_at,updated_at) "
            "VALUES (:id,'person-proposal',CAST(:proposal AS JSONB),'[]','pending','now','now')"), {"id": analysis,
            "proposal": json.dumps({"proposed_name": "Publisher identity", "member_ids": ["raw:Person publisher", f"canonical:{person['entity_id']}"]})})
    proposal = catalog.list_proposals(kind="identity")["items"][0]
    assert {row["snapshot"]["kind"] for row in catalog.proposal_detail(proposal["proposal_id"])["members"]} == {"person"}


def test_legacy_publisher_separation_uses_publisher_role_for_homonym(integrated_catalog):
    catalog, _ = integrated_catalog
    doc = catalog.create_document("7" * 32, actor="test")
    catalog.apply_metadata(doc["md5"], {"@type": "Book",
        "publisher": {"@type": "Organization", "name": "Shared"},
        "author": {"@type": "Person", "name": "Shared"}}, actor="AI", automated=True)
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO publisher_separations(left_key,right_key,provenance,created_at) "
            "VALUES ('raw:Other','raw:Shared','{}','now')"))
        shared = conn.execute(text("SELECT name_id FROM catalog_names WHERE kind='organization' AND raw_name='Shared'")).scalar_one()
        other = conn.execute(text("SELECT name_id FROM catalog_names WHERE kind='organization' AND raw_name='Other'")).scalar_one()
        row = conn.execute(text("SELECT left_key,right_key FROM catalog_separations")).one()
    assert set(row) == {f"name:{shared}", f"name:{other}"}

@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_lean_admin_alias_edits_keep_worker_review_inventory_current(integrated_catalog):
    catalog, _ = integrated_catalog
    person = catalog.create_entity('person', 'Initial', actor='owner', approval='confirmed')
    alias = catalog.add_alias(person['entity_id'], 'Observed', revision=1, actor='owner')
    with catalog.engine.connect() as conn:
        row = conn.execute(text("SELECT canonical_id,decision_status FROM normalization_aliases WHERE raw_name='Observed'")).mappings().one()
    assert row['canonical_id'] == person['entity_id']
    assert row['decision_status'] == 'linked'
    other = catalog.create_entity('person', 'Homonym', actor='owner', approval='confirmed')
    catalog.add_alias(other['entity_id'], 'Observed', revision=1, actor='owner')
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT canonical_id FROM normalization_aliases WHERE raw_name='Observed'")).scalar_one() is None
    catalog.remove_alias(alias['alias_id'], revision=1, actor='owner')
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT canonical_id FROM normalization_aliases WHERE raw_name='Observed'")).scalar_one() == other['entity_id']
    current = catalog.get('entity', other['entity_id'])
    catalog.patch('entity', other['entity_id'], {'revision': current['revision'], 'display_name': 'Renamed'}, actor='owner')
    with catalog.engine.connect() as conn:
        assert conn.execute(text('SELECT normalized_name FROM normalization_canonicals WHERE canonical_id=:id'), {'id':other['entity_id']}).scalar_one() == 'renamed'

@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_lean_classification_owner_can_swap_paths(integrated_catalog, monkeypatch):
    from app.modules.library import classification_editor as editor
    catalog, _ = integrated_catalog
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO classification(ddc,path_en,path_en_key) VALUES ('800','[\"Alpha\"]','alpha'),('800','[\"Beta\"]','beta')"))
        ids = conn.execute(text('SELECT id FROM classification ORDER BY id')).scalars().all()
    monkeypatch.setattr(editor, 'create_runtime_engine', lambda: (catalog.engine, 'test'))
    monkeypatch.setattr(editor, 'dispose_runtime_engine', lambda engine: None)
    payload = {'changes': [{'classification_id':ids[0], 'path':['Beta']}, {'classification_id':ids[1], 'path':['Alpha']}], 'merges':[]}
    with catalog.engine.connect() as conn:
        payload['base_revision'] = editor._taxonomy_revision(editor._all_rows(conn))
    plan = editor.preview_classification_change_set(payload)
    editor.apply_classification_change_set({**payload,'confirmed':True,'change_set_hash':plan['change_set_hash']})
    with catalog.engine.connect() as conn:
        assert conn.execute(text('SELECT path_en FROM classification ORDER BY id')).scalars().all() == [['Beta'], ['Alpha']]

@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_lean_command_rejects_stale_document_snapshot(integrated_catalog):
    from concurrent.futures import ThreadPoolExecutor
    import time
    catalog, _ = integrated_catalog
    digest = '7' * 32
    catalog.create_document(digest, mime_type='application/pdf', actor='test')
    with catalog.engine.begin() as first:
        first.execute(text('SELECT md5 FROM catalog_documents WHERE md5=:md5 FOR UPDATE'), {'md5':digest})
        with ThreadPoolExecutor(1) as pool:
            def worker():
                with catalog.engine.begin() as conn:
                    conn.execute(text("UPDATE document SET content_extraction_method='worker' WHERE md5=:md5"), {'md5':digest})
            future=pool.submit(worker)
            time.sleep(0.15)
            first.execute(text("UPDATE catalog_documents SET mime_type='text/plain',revision=revision+1 WHERE md5=:md5"), {'md5':digest})
            first.commit()
            from sqlalchemy.exc import DBAPIError
            with pytest.raises(DBAPIError) as error:
                future.result(timeout=10)
            assert error.value.orig.pgcode == '40001'
    assert catalog.get('document',digest)['mime_type']=='text/plain'

@pytest.mark.parametrize('integrated_catalog', ['essential'], indirect=True)
def test_lean_existing_collection_edit_merge_and_apply_commands(integrated_catalog, monkeypatch):
    from types import SimpleNamespace
    from app.modules.library import collection_catalog as collections
    catalog, _ = integrated_catalog
    monkeypatch.setenv('MANZARA_DB_SCHEMA',catalog.schema)
    monkeypatch.setattr(collections,'create_runtime_engine',lambda:(catalog.engine,'test'))
    monkeypatch.setattr(collections,'dispose_runtime_engine',lambda engine:None)
    events=[]
    db=SimpleNamespace(insert_event=lambda *args,**kwargs:events.append((args,kwargs)))
    first=catalog.create_collection('First: Books',actor='owner')
    second=catalog.create_collection('Second',actor='owner')
    digest='8'*32
    doc=catalog.create_document(digest,actor='test')
    catalog.apply_metadata(digest,{'@type':'Book','name':'Volume'},actor='AI',automated=True)
    with catalog.engine.begin() as conn:
        conn.execute(text("SELECT set_config('manzara.catalog_actor','owner',true)"))
        conn.execute(text('INSERT INTO library_collection_items(collection_id,md5) VALUES (:id,:md5)'),{'id':first['collection_id'],'md5':digest})
    collections.update_collection(db,first['collection_id'],{'title':'Renamed: Books','include_in_library':False})
    with catalog.engine.connect() as conn:
        assert conn.execute(text('SELECT normalized_title FROM library_collections WHERE collection_id=:id'),{'id':first['collection_id']}).scalar_one()=='renamed books'
    result=collections.merge_collections(db,source_collection_id=first['collection_id'],target_collection_id=second['collection_id'])
    assert result['moved_items']==1
    assert catalog.get('publication',doc['publication_id'])['collection_id']==second['collection_id']
    assert events

@pytest.mark.parametrize('staged_catalog', ['essential'], indirect=True)
def test_retirement_rejects_untracked_import_mapping_damage(staged_catalog):
    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        fingerprint=conn.execute(text('SELECT source_fingerprint FROM catalog_imports')).scalar_one()
        conn.execute(text("INSERT INTO catalog_entities(kind,display_name) VALUES ('person','Untracked')"))
    with pytest.raises(ValueError,match='retirement verification failed'):
        activate_catalog(catalog,reviewed_fingerprint=fingerprint,dataset_schema=catalog.schema,retire_legacy=True)
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT relkind FROM pg_class WHERE oid='document'::regclass")).scalar_one()=='r'
        assert conn.execute(text('SELECT state FROM catalog_imports')).scalar_one()=='verified'

def test_bulk_import_can_defer_search_indexes_only_in_empty_staging(staged_catalog):
    from app.catalog.search_indexes import defer_search_indexes,restore_search_indexes
    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values()
                          if table.name != 'catalog_imports')
        conn.execute(text(f'TRUNCATE {tables}'))
    defer_search_indexes(catalog)
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE schemaname=:schema AND indexname LIKE 'idx_catalog%trgm'"),{'schema':catalog.schema}).scalar_one()==0
        assert conn.execute(text("SELECT relkind FROM pg_class WHERE oid='document'::regclass")).scalar_one()=='r'
    catalog.create_document('6'*32,actor='test')
    with pytest.raises(ValueError,match='empty staging'):
        defer_search_indexes(catalog)
    restore_search_indexes(catalog)
    with catalog.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE schemaname=:schema AND indexname LIKE 'idx_catalog%trgm'"),{'schema':catalog.schema}).scalar_one()==8


def test_manual_staging_cleanup_rechecks_rows_and_preserves_sources(staged_catalog):
    from scripts.catalog_sql import render_empty_staging_reset
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    statement = render_empty_staging_reset(catalog.schema)
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO document(md5) VALUES (:md5)"), {"md5": "7" * 32})
    with pytest.raises(DBAPIError, match="staging contains rows"):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_imports')).scalar_one() == 1
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
            assert conn.execute(text('SELECT md5 FROM document')).scalar_one() == "7" * 32
            for table in catalog.tables.values():
                assert conn.execute(text(f'SELECT count(*) FROM "{catalog.schema}"."{table.name}"')).scalar_one() == 0


def test_editor_buffer_uses_standalone_queries_and_preserves_sources(staged_catalog):
    from scripts.catalog_sql import render_editor_buffer

    catalog, _ = staged_catalog
    statements = [part.strip() for part in render_editor_buffer(catalog.schema, catalog.schema).split(';') if part.strip()]
    assert len(statements) == 3
    assert not any('DO $' in statement for statement in statements)
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO document(md5) VALUES (:md5)"), {"md5": "7" * 32})
        result = json.loads(conn.execute(text(statements[0])).scalar_one())
        assert result['can_cleanup'] is False
        # Clear the populated test fixture, then exercise the empty-only workflow.
        conn.execute(text(statements[1]))
    for _ in range(2):
        with catalog.engine.begin() as conn:
            result = json.loads(conn.execute(text(statements[0])).scalar_one())
            assert result['can_cleanup'] is True
            conn.execute(text(statements[1]))
            result = json.loads(conn.execute(text(statements[2])).scalar_one())
            assert not any(result['staging_rows'].values())
            assert result['original_documents'] == 1
            assert conn.execute(text("SELECT relkind FROM pg_class WHERE oid='document'::regclass")).scalar_one() == 'r'


def test_editor_initializer_is_atomic_resumable_and_survives_semicolon_splitting(staged_catalog):
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source), 'evidence_mode': 'essential', 'manual_editor': True,
                'source_tables': {name: {'rows': len(rows), 'fingerprint': snapshot_fingerprint({name: rows})}
                                  for name, rows in source.items()}}
    statement = render_manual_initialize(fingerprint, manifest, catalog.schema, catalog.schema)
    assert statement.count(';') == 1
    with pytest.raises(DBAPIError, match='another or completed import'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        conn.execute(text("INSERT INTO catalog_names(kind,raw_name) VALUES ('person','Unexpected staging row')"))
    with pytest.raises(DBAPIError, match='staging contains rows'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_imports')).scalar_one() == 0
        conn.execute(text('DELETE FROM catalog_names'))
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO document(md5) VALUES (:md5)"), {'md5': '7' * 32})
    with pytest.raises(DBAPIError, match='source count changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        conn.execute(text('DELETE FROM document WHERE md5=:md5'), {'md5': '7' * 32})
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
            row = conn.execute(text('SELECT * FROM catalog_imports')).mappings().one()
            assert row['state'] == 'loading'
            assert row['source_fingerprint'] == fingerprint
            assert row['manifest'] == {**manifest, 'dataset_schema': catalog.schema,
                                       'manual_completed_steps': ['initialized']}
            assert conn.execute(text("SELECT relkind FROM pg_class WHERE oid='document'::regclass")).scalar_one() == 'r'
    with pytest.raises(ValueError, match='not verified'):
        import_snapshot(catalog, source, actor='migration', evidence_mode='essential')
    with pytest.raises(ValueError, match='not verified'):
        activate_catalog(catalog, reviewed_fingerprint=fingerprint, dataset_schema=catalog.schema, retire_legacy=True)


def test_editor_foundation_preserves_approvals_merges_and_collections_atomically(staged_catalog):
    from scripts.catalog_manual_foundation import render_manual_foundation
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot, _confirmed_entities
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        for key, kind, name, status, target in [
            (1001, 'publisher', "Publisher; owner's", 'active', None),
            (1002, 'personality', 'Person', 'active', None),
            (1003, 'publisher', 'Merged publisher', 'merged', 1001),
            (1004, 'publisher', 'Reverted publisher', 'active', None),
            (1005, 'publisher', 'Other event', 'active', None),
            (1006, 'publisher', 'Untouched publisher', 'active', None),
            (1007, 'publisher', 'Renamed publisher', 'active', None),
        ]:
            conn.execute(text('''INSERT INTO normalization_canonicals(canonical_id,entity_type,
                display_name,normalized_name,status,merged_into_id,created_at,updated_at,notes)
                VALUES (:key,:kind,:name,:name,:status,:target,'created','updated','source notes')'''),
                {'key': key, 'kind': kind, 'name': name, 'status': status, 'target': target})
        events = [
            ('apply_publisher_change_set', 0, {'after': {'canonicals': [
                {'canonical_id': 1001, 'display_name': "Publisher; owner's"},
                {'canonical_id': 1006, 'display_name': 'Untouched publisher'},
                {'canonical_id': 1007, 'display_name': 'Old name'},
            ]}, 'touched_canonical_ids': [1001, 1007]}),
            ('apply_personality_change_set', 0, {'renames': [{'canonical_id': 1002, 'display_name': 'Person'}]}),
            ('apply_publisher_change_set', 1, {'renames': [{'canonical_id': 1004, 'display_name': 'Reverted publisher'}]}),
            ('rename_canonical', 0, {'renames': [{'canonical_id': 1005, 'display_name': 'Other event'}]}),
        ]
        for action, reverted, payload in events:
            conn.execute(text('''INSERT INTO normalization_events(entity_type,action,payload_json,reverted,created_at)
                VALUES ('publisher',:action,:payload,:reverted,'event-created')'''),
                {'action': action, 'payload': json.dumps(payload), 'reverted': reverted})
        conn.execute(text('''INSERT INTO library_collections(collection_id,title,normalized_title,
            include_in_library,metadata_template_json,created_at,updated_at,applied_at,notes)
            VALUES (2001,'Collection; original','explicit-normalized',0,'{"name":"Template"}',
            'created','updated','applied','notes')'''))
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source), 'confirmed_entities': len(_confirmed_entities(source)),
                'evidence_mode': 'essential', 'manual_editor': True,
                'source_tables': {name: {'rows': len(rows), 'fingerprint': snapshot_fingerprint({name: rows})}
                                  for name, rows in source.items()}}
    statement = render_manual_foundation(fingerprint, catalog.schema, catalog.schema)
    assert statement.count(';') == 1
    with pytest.raises(DBAPIError, match='reviewed manual loading import required'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_initialize(fingerprint, manifest, catalog.schema, catalog.schema)))
        conn.execute(text('UPDATE library_collections SET include_in_library=2'))
    with pytest.raises(DBAPIError, match='invalid collection inclusion'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_entities')).scalar_one() == 0
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == ['initialized']
        conn.execute(text('UPDATE library_collections SET include_in_library=0'))
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
            assert set(conn.execute(text("SELECT entity_id FROM catalog_entities WHERE approval='confirmed'")).scalars()) == _confirmed_entities(source)
            assert conn.execute(text('SELECT count(*) FROM catalog_entity_roles')).scalar_one() == 7
            assert conn.execute(text('SELECT merged_into_id FROM catalog_entities WHERE entity_id=1003')).scalar_one() == 1001
            collection = conn.execute(text('SELECT * FROM catalog_collections')).mappings().one()
            assert collection['include_in_library'] is False
            assert collection['metadata_template_json'] == '{"name":"Template"}'
            assert collection['normalized_title'] == 'explicit-normalized'
            assert collection['source_updated_at'] == 'updated'
            assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == ['initialized', 'foundation']
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)) == fingerprint
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_entities SET display_name='Untracked edit' WHERE entity_id=1001"))
    with pytest.raises(DBAPIError, match='foundation mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))


def test_editor_taxonomy_preserves_casefolded_branches_and_refuses_changed_sources(staged_catalog):
    from scripts.catalog_manual_taxonomy import render_manual_taxonomy
    from scripts.catalog_manual_foundation import render_manual_foundation
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        for key, ddc, path, translated in [
            (101, '800', ['Straße', "First; owner's branch"], None),
            (102, '800', ['STRASSE', 'Second'], ['Shared translation', 'Second translation']),
            (103, '900', ['Straße', 'Third'], ['Different DDC', 'Third translation']),
        ]:
            conn.execute(text('''INSERT INTO classification(id,ddc,path_en,path_en_key,path_tt,status,created_by,created_at)
                VALUES (:key,:ddc,CAST(:path AS JSON),:path,CAST(:tt AS JSON),'confirmed','owner','2026-01-02 03:04:05.123456')'''),
                {'key': key, 'ddc': ddc, 'path': json.dumps(path), 'tt': json.dumps(translated)})
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source), 'confirmed_entities': 0,
                'evidence_mode': 'essential', 'manual_editor': True,
                'source_tables': {name: {'rows': len(rows), 'fingerprint': snapshot_fingerprint({name: rows})}
                                  for name, rows in source.items()}}
    with catalog.engine.connect() as conn:
        classification_fingerprint = conn.execute(text("""SELECT encode(sha256(convert_to(
            coalesce(jsonb_agg(to_jsonb(c) ORDER BY id),'[]')::text,'UTF8')),'hex') FROM classification c""")).scalar_one()
    statement = render_manual_taxonomy(fingerprint, source['classification'], catalog.schema, catalog.schema,
                                      classification_fingerprint=classification_fingerprint)
    assert statement.count(';') == 1
    assert 'first_classification' in statement
    assert 'path_en_key' not in statement
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_initialize(fingerprint, manifest, catalog.schema, catalog.schema)))
    with pytest.raises(DBAPIError, match='taxonomy batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_foundation(fingerprint, catalog.schema, catalog.schema)))
        conn.execute(text("UPDATE classification SET created_by='unreviewed edit' WHERE id=101"))
    with pytest.raises(DBAPIError, match='reviewed classification source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_classification_nodes')).scalar_one() == 0
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == ['initialized', 'foundation']
        conn.execute(text("UPDATE classification SET created_by='owner' WHERE id=101"))
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
            nodes = conn.execute(text('SELECT * FROM catalog_classification_nodes ORDER BY node_id')).mappings().all()
            assert len(nodes) == 5
            assert nodes[0]['label_en'] == 'Straße' and nodes[0]['label_tt'] == 'Shared translation'
            assert nodes[1]['parent_id'] == nodes[2]['parent_id'] == nodes[0]['node_id']
            assert nodes[3]['ddc'] == '900' and nodes[3]['label_tt'] == 'Different DDC'
            rows = conn.execute(text('SELECT * FROM catalog_classifications ORDER BY classification_id')).mappings().all()
            assert [row['classification_id'] for row in rows] == [101, 102, 103]
            assert all(row['created_by'] == 'owner' and row['status'] == 'confirmed' for row in rows)
            assert rows[0]['created_at'] == source['classification'][0]['created_at']
            assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == ['initialized', 'foundation', 'taxonomy']
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)) == fingerprint
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_classification_nodes SET label_tt='untracked edit' WHERE node_id=1"))
    with pytest.raises(DBAPIError, match='taxonomy mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))


@pytest.mark.parametrize('paths', [
    [{'id': 1, 'ddc': '1', 'path_en': [], 'path_tt': []}],
    [{'id': 1, 'ddc': '1', 'path_en': [' '], 'path_tt': []}],
    [{'id': 1, 'ddc': '1', 'path_en': ['Root', 'A'], 'path_tt': ['First']},
     {'id': 2, 'ddc': '1', 'path_en': ['ROOT', 'B'], 'path_tt': ['Conflicting']}],
])
def test_manual_taxonomy_rejects_invalid_or_conflicting_paths(paths):
    from scripts.catalog_manual_taxonomy import render_manual_taxonomy

    with pytest.raises(ValueError, match='classification path|conflicting translations'):
        render_manual_taxonomy('0' * 64, paths, classification_fingerprint='0' * 64)


def test_editor_alias_seed_preserves_reviews_and_resolves_merged_identities(staged_catalog):
    from scripts.catalog_manual_aliases import render_manual_aliases
    from scripts.catalog_manual_foundation import render_manual_foundation
    from scripts.catalog_manual_taxonomy import render_manual_taxonomy
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        for key, kind, target, status in [(100, 'publisher', None, 'active'), (101, 'publisher', 100, 'merged'),
                                           (102, 'personality', 101, 'merged'), (103, 'personality', None, 'active')]:
            conn.execute(text('''INSERT INTO normalization_canonicals(canonical_id,entity_type,display_name,
                normalized_name,merged_into_id,status,created_at,updated_at)
                VALUES (:key,:kind,:name,:name,:target,:status,'created','updated')'''),
                {'key': key, 'kind': kind, 'name': f'Identity {key}', 'target': target, 'status': status})
        conn.execute(text('''INSERT INTO normalization_events(entity_type,action,payload_json,reverted,created_at)
            VALUES ('publisher','apply_publisher_change_set',:payload,0,'event-created')'''),
            {'payload': json.dumps({'renames': [{'canonical_id': 100, 'display_name': 'Identity 100'}]})})
        for key, kind, raw, target, status in [
            (205, 'personality', "Shared; owner's name", 102, 'linked'),
            (201, 'publisher', "Shared; owner's name", 101, 'linked'),
            (202, 'personality', 'Pending', None, 'pending'),
            (203, 'personality', 'Unconfirmed', 103, 'linked'),
        ]:
            conn.execute(text('''INSERT INTO normalization_aliases(alias_id,entity_type,raw_name,
                normalized_name,script_label,canonical_id,decision_status,docs_count,mentions_count,marker_count,
                confidence,source,reason,created_at,updated_at,source_roles,successful_model,prompt_version,schema_version,
                surname_full,name_initials,title,sex)
                VALUES (:key,:kind,:raw,:raw,'latin',:target,:status,11,22,33,0.625,'source','reason',
                'created','source-updated',CAST(:roles AS JSONB),'model','prompt','schema','Surname','A.','Title','unknown')'''),
                {'key': key, 'kind': kind, 'raw': raw, 'target': target, 'status': status,
                 'roles': json.dumps(['author', 'publisher'])})
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source), 'confirmed_entities': 1,
                'evidence_mode': 'essential', 'manual_editor': True,
                'source_tables': {name: {'rows': len(rows), 'fingerprint': snapshot_fingerprint({name: rows})}
                                  for name, rows in source.items()}}
    with catalog.engine.connect() as conn:
        signatures = {table: conn.execute(text(f"""SELECT encode(sha256(convert_to(
            coalesce(jsonb_agg(to_jsonb(c) ORDER BY {key}),'[]')::text,'UTF8')),'hex') FROM {table} c""")).scalar_one()
            for table, key in [('classification', 'id'), ('normalization_aliases', 'alias_id')]}
    statement = render_manual_aliases(fingerprint, catalog.schema, catalog.schema,
                                     aliases_fingerprint=signatures['normalization_aliases'])
    assert statement.count(';') == 1
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_initialize(fingerprint, manifest, catalog.schema, catalog.schema)))
        conn.execute(text(render_manual_foundation(fingerprint, catalog.schema, catalog.schema)))
    with pytest.raises(DBAPIError, match='alias batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_taxonomy(fingerprint, source['classification'], catalog.schema, catalog.schema,
                                               classification_fingerprint=signatures['classification'])))
        conn.execute(text("UPDATE normalization_aliases SET reason='unreviewed edit' WHERE alias_id=201"))
    with pytest.raises(DBAPIError, match='reviewed alias source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_names')).scalar_one() == 0
        conn.execute(text("UPDATE normalization_aliases SET reason='reason' WHERE alias_id=201"))
        conn.execute(text("UPDATE normalization_canonicals SET merged_into_id=102,status='merged' WHERE canonical_id=100"))
        conn.execute(text("UPDATE catalog_entities SET merged_into_id=102,status='merged' WHERE entity_id=100"))
    with pytest.raises(DBAPIError, match='identity merge cycle'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_names')).scalar_one() == 0
        conn.execute(text("UPDATE normalization_canonicals SET merged_into_id=NULL,status='active' WHERE canonical_id=100"))
        conn.execute(text("UPDATE catalog_entities SET merged_into_id=NULL,status='active' WHERE entity_id=100"))
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
            reviews = {row['alias_id']: dict(row) for row in conn.execute(text('SELECT * FROM catalog_alias_reviews')).mappings()}
            assert set(reviews) == {201, 202, 203, 205}
            for old in source['normalization_aliases']:
                actual = reviews[old['alias_id']]
                assert {key: actual[key] for key in old if key not in {'raw_name', 'canonical_id'}} == {
                    key: value for key, value in old.items() if key not in {'raw_name', 'canonical_id'}}
                assert actual['entity_id'] == old['canonical_id']
            aliases = conn.execute(text('''SELECT n.kind,n.raw_name,a.entity_id,a.approval
                FROM catalog_aliases a JOIN catalog_names n USING(name_id) ORDER BY a.entity_id''')).mappings().all()
            assert [(row['kind'],row['entity_id'],row['approval']) for row in aliases] == [
                ('organization', 100, 'confirmed'), ('person', 103, 'unconfirmed')]
            assert conn.execute(text('SELECT count(*) FROM catalog_names')).scalar_one() == 4
            assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == [
                'initialized', 'foundation', 'taxonomy', 'aliases']
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)) == fingerprint
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO catalog_names(name_id,kind,raw_name) VALUES (999,'person','Later metadata name')"))
    with catalog.engine.begin() as conn:
        conn.execute(text(statement))
        assert conn.execute(text('SELECT count(*) FROM catalog_names')).scalar_one() == 5
        conn.execute(text("UPDATE catalog_alias_reviews SET updated_at='untracked edit' WHERE alias_id=201"))
    with pytest.raises(DBAPIError, match='alias mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))


def test_editor_document_batches_resume_and_preserve_metadata_and_privacy(staged_catalog):
    from scripts.catalog_manual_documents import render_manual_documents, document_source_signature_query
    from scripts.catalog_manual_aliases import render_manual_aliases
    from scripts.catalog_manual_foundation import render_manual_foundation
    from scripts.catalog_manual_taxonomy import render_manual_taxonomy
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    payload = {'@type': 'Book', 'name': "Owner's; book", 'datePublished': '1999-07', 'numberOfPages': 19,
               'inLanguage': '\u00a0tt\u2003, en\u3000', 'accessMode': ['textual', 'visual'],
               'audience': [{'@type': 'PeopleAudience', 'suggestedMinAge': 12}]}
    with catalog.engine.begin() as conn:
        # Production's original Sheets-era metadata column is JSON rather than JSONB.
        conn.execute(text('ALTER TABLE metadata ALTER COLUMN schema_org TYPE JSON USING schema_org::json'))
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        conn.execute(text("""INSERT INTO library_collections(collection_id,title,normalized_title,created_at,updated_at)
            VALUES (9,'Collection','collection','created','updated')"""))
        conn.execute(text("""INSERT INTO classification(id,ddc,path_en,path_en_key,path_tt)
            VALUES (101,'800','["Root"]','root','["Translated"]')"""))
        for key, language, complete, restricted in [('a', 'tt', True, False), ('b', '\u00a0en, fr\u3000,', None, None),
                                                     ('c', None, False, True), ('d', None, True, False), ('e', 'en', True, None)]:
            conn.execute(text('''INSERT INTO document(md5,mime_type,language,"full",sharing_restricted,
                content_extraction_method,meta_extraction_method)
                VALUES (:md5,'application/pdf',:language,:complete,:restricted,'content-method','metadata-method')'''),
                {'md5': key * 32, 'language': language, 'complete': complete, 'restricted': restricted})
        for key, data, included, classification in [
            ('a', payload, True, 101), ('b', None, False, None),
            ('d', {'@type': 'CreativeWork', 'inLanguage': 'en', 'accessMode': 'textual',
                   'audience': {'@type': 'PeopleAudience', 'suggestedMinAge': 12}}, None, None),
            ('e', None, None, None),
        ]:
            conn.execute(text('''INSERT INTO metadata(md5,schema_org,lib,lib_eval_method,classification_id)
                VALUES (:md5,CAST(:payload AS JSONB),:included,'reviewed',:classification)'''),
                {'md5': key * 32, 'payload': json.dumps(data) if data is not None or key == 'e' else None,
                 'included': included, 'classification': classification})
        conn.execute(text('''INSERT INTO library_collection_items(collection_id,md5,item_title,created_at,updated_at)
            VALUES (9,:md5,'Distinct item title','item-created','item-updated')'''), {'md5': 'a' * 32})
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source), 'confirmed_entities': 0,
                'evidence_mode': 'essential', 'manual_editor': True,
                'source_tables': {name: {'rows': len(rows), 'fingerprint': snapshot_fingerprint({name: rows})}
                                  for name, rows in source.items()}}
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_initialize(fingerprint, manifest, catalog.schema, catalog.schema)))
        conn.execute(text(render_manual_foundation(fingerprint, catalog.schema, catalog.schema)))
        def native(table, key):
            return conn.execute(text(f"""SELECT encode(sha256(convert_to(
                coalesce(jsonb_agg(to_jsonb(c) ORDER BY {key}),'[]')::text,'UTF8')),'hex') FROM {table} c""")).scalar_one()
        conn.execute(text(render_manual_taxonomy(fingerprint, source['classification'], catalog.schema, catalog.schema,
                                               classification_fingerprint=native('classification', 'id'))))
        conn.execute(text(render_manual_aliases(fingerprint, catalog.schema, catalog.schema,
                                              aliases_fingerprint=native('normalization_aliases', 'alias_id'))))

    def statement(lower, upper, offset, rows, final):
        with catalog.engine.connect() as conn:
            signatures = {table: conn.execute(text(document_source_signature_query(catalog.schema, table, lower, upper))).scalar_one()
                          for table in ('document', 'metadata', 'library_collection_items')}
        return render_manual_documents(fingerprint, catalog.schema, catalog.schema,
                                       lower_md5=lower, upper_md5=upper, offset=offset, rows=rows,
                                       final_batch=final, source_signatures=signatures)

    first = statement(None, 'b' * 32, 0, 2, False)
    last = statement('b' * 32, 'e' * 32, 2, 3, True)
    assert first.count(';') == last.count(';') == 1
    with pytest.raises(DBAPIError, match='document batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(last))
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE library_collection_items SET item_title='unreviewed edit'"))
    with pytest.raises(DBAPIError, match='document batch source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(first))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_publications')).scalar_one() == 0
        conn.execute(text("UPDATE library_collection_items SET item_title='Distinct item title'"))
        conn.execute(text("UPDATE metadata SET schema_org=jsonb_set(schema_org::jsonb,'{numberOfPages}','\"19\"') WHERE md5=:md5"), {'md5': 'a' * 32})
    invalid = statement(None, 'b' * 32, 0, 2, False)
    with pytest.raises(DBAPIError, match='invalid bibliographic scalar'):
        with catalog.engine.begin() as conn:
            conn.execute(text(invalid))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_publications')).scalar_one() == 0
        conn.execute(text('UPDATE metadata SET schema_org=CAST(:payload AS JSONB) WHERE md5=:md5'),
                     {'payload': json.dumps(payload), 'md5': 'a' * 32})
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.execute(text(first))
            assert conn.execute(text('SELECT count(*) FROM catalog_documents')).scalar_one() == 2
            progress = conn.execute(text("SELECT manifest->'manual_document_progress' FROM catalog_imports")).scalar_one()
            assert progress == {'loaded_documents': 2, 'last_md5': 'b' * 32}
            assert 'documents' not in conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one()
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE metadata SET schema_org=jsonb_set(schema_org::jsonb,'{name}','\"unreviewed edit\"') WHERE md5=:md5"),
                     {'md5': 'd' * 32})
    with pytest.raises(DBAPIError, match='document batch source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(last))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_documents')).scalar_one() == 2
        assert conn.execute(text("SELECT manifest->'manual_document_progress' FROM catalog_imports")).scalar_one() == {
            'loaded_documents': 2, 'last_md5': 'b' * 32}
        original = next(row['schema_org'] for row in source['metadata'] if row['md5'] == 'd' * 32)
        conn.execute(text('UPDATE metadata SET schema_org=CAST(:payload AS JSONB) WHERE md5=:md5'),
                     {'payload': json.dumps(original), 'md5': 'd' * 32})
    for query in (last, first, last):
        with catalog.engine.begin() as conn:
            conn.execute(text(query))
    with catalog.engine.connect() as conn:
        records = {row['md5']: row for row in conn.execute(text('''SELECT d.*,p.name,p.work_type,p.date_published,p.page_count,
            p.languages,p.inclusion,p.classification_id,p.collection_id,p.collection_item_title,p.collection_created_at,
            p.collection_updated_at,p.has_metadata,p.metadata_present,p.audience_array
            FROM catalog_documents d JOIN catalog_publications p USING(publication_id)''')).mappings()}
        a, b, c, d, e = (records[key * 32] for key in 'abcde')
        assert [records[key * 32]['publication_id'] for key in 'abcde'] == [1, 2, 3, 4, 5]
        assert a['name'] == payload['name'] and a['page_count'] == 19 and a['date_published'] == '1999-07'
        assert a['languages'] == ['tt', 'en'] and a['access_modes'] == ['textual', 'visual']
        assert a['inclusion'] == 'included' and a['complete'] and not a['restricted']
        assert (a['classification_id'], a['collection_id'], a['collection_item_title']) == (101, 9, 'Distinct item title')
        assert (a['collection_created_at'], a['collection_updated_at']) == ('item-created', 'item-updated')
        assert a['audience_array'] and not d['audience_array'] and d['access_modes'] == ['textual']
        assert b['languages'] == ['en', 'fr'] and b['inclusion'] == 'excluded' and b['restricted'] and not b['complete']
        assert b['has_metadata'] and not b['metadata_present'] and b['work_type'] == 'CreativeWork'
        assert not c['has_metadata'] and not c['metadata_present'] and c['languages'] == [] and c['inclusion'] == 'pending'
        assert e['has_metadata'] and not e['metadata_present'] and e['languages'] == ['en'] and e['restricted']
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one() == [
            'initialized', 'foundation', 'taxonomy', 'aliases', 'documents']
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)) == fingerprint
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_publications SET name='Untracked edit' WHERE publication_id=1"))
    with pytest.raises(DBAPIError, match='document mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(first))


@pytest.mark.parametrize('changes', [
    {'offset': True}, {'offset': -1}, {'rows': False}, {'rows': 1.5}, {'rows': 0}, {'rows': 10001},
    {'final_batch': 'false'}, {'upper_md5': 'bad'}, {'lower_md5': 'c' * 32, 'offset': 1},
    {'source_signatures': {'document': '0' * 64}},
])
def test_manual_document_batches_validate_boundaries_and_limits(changes):
    from scripts.catalog_manual_documents import render_manual_documents

    args = {'lower_md5': None, 'upper_md5': 'b' * 32, 'offset': 0, 'rows': 2, 'final_batch': False,
            'source_signatures': {name: '0' * 64 for name in ('document', 'metadata', 'library_collection_items')}}
    with pytest.raises(ValueError):
        render_manual_documents('0' * 64, **{**args, **changes})


def test_editor_metadata_details_preserve_positions_nulls_and_source_terms(staged_catalog):
    from scripts.catalog_manual_details import render_manual_details
    from scripts.catalog_manual_documents import document_source_signature_query
    from sqlalchemy.exc import DBAPIError
    from app.catalog.metadata import decompose_metadata

    catalog, _ = staged_catalog
    def term(name, definition):
        return {'@type': 'DefinedTerm', 'name': name, 'termCode': name, 'inDefinedTermSet': definition}
    payload = {'@type': 'Book', 'name': "Owner's; detail", 'isbn': ['978-0-123456-47-2', '0-123456-47-x'],
               'genre': ['Poetry', 'Poetry'], 'about': [
                   term('Managed', {'@type': 'DefinedTermSet', 'name': 'DDC'}),
                   term('First', {'@type': 'DefinedTermSet', 'name': 'Subjects'}),
                   term('Managed path', {'@type': 'DefinedTermSet', 'name': 'CATEGORYPATH'}),
                   term('Second', 'https://example.test/terms')],
               'audience': [{'@type': 'PeopleAudience', 'suggestedMinAge': 12, 'suggestedMaxAge': 17}],
               'accessModeSufficient': [{'@type': 'ItemList', 'itemListElement': ['textual', 'visual']},
                                        {'@type': 'ItemList', 'itemListElement': ['auditory']}],
               'isBasedOn': {'@type': 'Book', 'name': 'Original', 'inLanguage': 'en',
                             'author': [{'@type': 'Person', 'name': 'First author'},
                                        {'@type': 'Organization', 'name': 'Second author'}]}}
    second = {'@type': 'CreativeWork', 'about': [term('Keep DDC', {'@type': 'DefinedTermSet', 'name': 'ddc'})],
              'isBasedOn': {'@type': 'Book', 'url': []}}
    with catalog.engine.begin() as conn:
        conn.execute(text('DELETE FROM catalog_imports'))
        conn.execute(text('''INSERT INTO classification(id,ddc,path_en,path_en_key) VALUES(101,'800','["Root"]','root')'''))
        conn.execute(text('''INSERT INTO catalog_classification_nodes(node_id,ddc,label_en) VALUES(1,'800','Root')'''))
        conn.execute(text('''INSERT INTO catalog_classifications(classification_id,node_id) VALUES(101,1)'''))
        for key, data, classification, identifier in [('a', payload, 101, 1), ('b', second, None, 2)]:
            conn.execute(text('INSERT INTO document(md5) VALUES(:md5)'), {'md5': key * 32})
            conn.execute(text('''INSERT INTO metadata(md5,schema_org,classification_id) VALUES(:md5,CAST(:payload AS JSONB),:classification)'''),
                         {'md5': key * 32, 'payload': json.dumps(data), 'classification': classification})
            record = decompose_metadata(data)
            conn.execute(catalog.table('publications').insert().values(publication_id=identifier, **record['scalars'],
                languages=record['languages'], audience_array=record['audience_array'], classification_id=classification))
            conn.execute(catalog.table('documents').insert().values(md5=key*32, publication_id=identifier,
                complete=False, restricted=True, access_modes=record['access_modes']))
        # This focused fixture starts at the preceding, already-verified document checkpoint.
        conn.execute(text('''INSERT INTO catalog_imports(source_fingerprint,state,manifest)
            VALUES(:fingerprint,'loading',CAST(:manifest AS JSONB))'''),
            {'fingerprint': '0' * 64, 'manifest': json.dumps({'manual_editor': True, 'evidence_mode': 'essential',
                'dataset_schema': catalog.schema, 'documents': 2,
                'manual_completed_steps': ['initialized','foundation','taxonomy','aliases','documents'],
                'manual_document_progress': {'loaded_documents': 2, 'last_md5': 'b' * 32},
                'source_tables': {'document': {'rows': 2}, 'metadata': {'rows': 2}, 'library_collection_items': {'rows': 0}}})})
    before = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)

    def command(lower, upper, offset, final):
        with catalog.engine.connect() as conn:
            signatures = {name: conn.execute(text(document_source_signature_query(catalog.schema, name, lower, upper))).scalar_one()
                          for name in ('document', 'metadata', 'library_collection_items')}
        return render_manual_details('0' * 64, catalog.schema, catalog.schema, lower_md5=lower, upper_md5=upper,
                                     offset=offset, rows=1, final_batch=final, source_signatures=signatures)

    first, last = command(None, 'a' * 32, 0, False), command('a' * 32, 'b' * 32, 1, True)
    assert first.count(';') == last.count(';') == 1
    with pytest.raises(DBAPIError, match='metadata detail batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(last))
    with catalog.engine.begin() as conn:
        conn.execute(text('UPDATE metadata SET schema_org=CAST(:payload AS JSONB) WHERE md5=:md5'),
                     {'payload': json.dumps({**payload, 'genre': ['Unreviewed edit']}), 'md5': 'a' * 32})
    with pytest.raises(DBAPIError, match='document batch source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(first))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_genres')).scalar_one() == 0
        conn.execute(text('UPDATE metadata SET schema_org=CAST(:payload AS JSONB) WHERE md5=:md5'),
                     {'payload': json.dumps(payload), 'md5': 'a' * 32})
    for query in [first, first, last, first, last]:
        with catalog.engine.begin() as conn:
            conn.execute(text(query))
    with catalog.engine.connect() as conn:
        assert conn.execute(text('SELECT value,normalized,position FROM catalog_identifiers ORDER BY position')).all() == [
            ('978-0-123456-47-2', '9780123456472', 0), ('0-123456-47-x', '012345647X', 1)]
        assert conn.execute(text('SELECT value,position FROM catalog_genres ORDER BY position')).all() == [('Poetry', 0), ('Poetry', 1)]
        subjects = conn.execute(text('SELECT name,set_is_url,set_url,position FROM catalog_subjects ORDER BY publication_id,position')).all()
        assert subjects == [('First', False, None, 0), ('Second', True, 'https://example.test/terms', 1), ('Keep DDC', False, None, 0)]
        assert conn.execute(text('SELECT kind,min_age,max_age,position FROM catalog_audiences')).one() == ('PeopleAudience', 12, 17, 0)
        assert conn.execute(text('SELECT modes,position FROM catalog_sufficient_modes ORDER BY position')).all() == [(['textual','visual'],0),(['auditory'],1)]
        assert conn.execute(text('SELECT urls FROM catalog_references ORDER BY publication_id')).scalars().all() == [None, []]
        assert conn.execute(text('SELECT kind,name,position FROM catalog_reference_authors ORDER BY position')).all() == [
            ('Person','First author',0),('Organization','Second author',1)]
        assert conn.execute(text("SELECT manifest->'manual_details_progress' FROM catalog_imports")).scalar_one() == {'loaded_documents':2,'last_md5':'b'*32}
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one()[-1] == 'metadata_details'
    from app.catalog.importer import snapshot_fingerprint
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)) == snapshot_fingerprint(before)
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_genres SET value='Untracked edit' WHERE position=0"))
    with pytest.raises(DBAPIError, match='metadata detail mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(first))


def test_editor_credit_batches_reuse_names_preserve_roles_and_verify_retries(staged_catalog):
    from scripts.catalog_manual_credits import render_manual_credits
    from scripts.catalog_manual_aliases import render_manual_aliases
    from scripts.catalog_manual_details import render_manual_details
    from scripts.catalog_manual_documents import document_source_signature_query, render_manual_documents
    from scripts.catalog_manual_foundation import render_manual_foundation
    from scripts.catalog_manual_taxonomy import render_manual_taxonomy
    from scripts.catalog_sql import render_manual_initialize
    from app.catalog.importer import snapshot_fingerprint, validate_snapshot
    from app.catalog.metadata import decompose_metadata
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    payloads = [
        {'@type': 'Book', 'author': [{'@type': 'Person', 'name': 'Known person'},
            {'@type': 'Person', 'name': 'New name'}, {'@type': 'Person', 'name': 'New name'}],
         'contributor': {'@type': 'Role', 'roleName': 'Proofreading',
                         'contributor': {'@type': 'Organization', 'name': 'New name'}},
         'publisher': {'@type': 'Organization', 'name': 'Known publisher'}},
        {'@type': 'Book', 'author': [{'@type': 'Person', 'name': 'New name'},
                                   {'@type': 'Person', 'name': 'Later name'}],
         'publisher': {'@type': 'Organization', 'name': 'New name'}},
    ]
    with catalog.engine.begin() as conn:
        tables = ','.join(f'"{catalog.schema}"."{table.name}"' for table in catalog.tables.values())
        conn.execute(text(f'TRUNCATE {tables} RESTART IDENTITY RESTRICT'))
        for key, kind, target, status in [(1,'publisher',None,'active'), (2,'publisher',1,'merged'), (3,'personality',None,'active')]:
            conn.execute(text('''INSERT INTO normalization_canonicals(canonical_id,entity_type,display_name,
                normalized_name,merged_into_id,status,created_at,updated_at)
                VALUES(:key,:kind,:name,:name,:target,:status,'created','updated')'''),
                {'key':key,'kind':kind,'name':f'Identity {key}','target':target,'status':status})
        conn.execute(text('''INSERT INTO normalization_events(entity_type,action,payload_json,reverted,created_at)
            VALUES('publisher','apply_publisher_change_set',:payload,0,'created')'''),
            {'payload':json.dumps({'renames':[{'canonical_id':1,'display_name':'Identity 1'}]})})
        for key, kind, raw, target in [(10,'publisher','Known publisher',2),(11,'personality','Known person',3)]:
            conn.execute(text('''INSERT INTO normalization_aliases(alias_id,entity_type,raw_name,normalized_name,
                script_label,canonical_id,decision_status,created_at,updated_at)
                VALUES(:key,:kind,:raw,:raw,'latin',:target,'linked','created','updated')'''),
                {'key':key,'kind':kind,'raw':raw,'target':target})
        for key, payload in zip(['a','b'],payloads):
            conn.execute(text('INSERT INTO document(md5) VALUES(:md5)'),{'md5':key*32})
            conn.execute(text('INSERT INTO metadata(md5,schema_org) VALUES(:md5,CAST(:payload AS JSONB))'),
                         {'md5':key*32,'payload':json.dumps(payload)})
    source = read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)
    fingerprint = snapshot_fingerprint(source)
    manifest = {**validate_snapshot(source),'confirmed_entities':1,'evidence_mode':'essential','manual_editor':True,
                'source_tables':{name:{'rows':len(rows),'fingerprint':snapshot_fingerprint({name:rows})} for name,rows in source.items()}}
    with catalog.engine.connect() as conn:
        alias_fp = conn.execute(text("SELECT encode(sha256(convert_to(jsonb_agg(to_jsonb(c) ORDER BY alias_id)::text,'UTF8')),'hex') FROM normalization_aliases c")).scalar_one()
        class_fp = conn.execute(text("SELECT encode(sha256(convert_to('[]','UTF8')),'hex')")).scalar_one()
        controls = []
        for offset,key in enumerate(['a','b']):
            lower,upper = (None if not offset else 'a'*32),key*32
            signatures = {name:conn.execute(text(document_source_signature_query(catalog.schema,name,lower,upper))).scalar_one()
                          for name in ('document','metadata','library_collection_items')}
            controls.append(dict(lower_md5=lower,upper_md5=upper,offset=offset,rows=1,final_batch=bool(offset),source_signatures=signatures))
    with catalog.engine.begin() as conn:
        conn.execute(text(render_manual_initialize(fingerprint,manifest,catalog.schema,catalog.schema)))
        conn.execute(text(render_manual_foundation(fingerprint,catalog.schema,catalog.schema)))
        conn.execute(text(render_manual_taxonomy(fingerprint,[],catalog.schema,catalog.schema,classification_fingerprint=class_fp)))
        conn.execute(text(render_manual_aliases(fingerprint,catalog.schema,catalog.schema,aliases_fingerprint=alias_fp)))
    for renderer in [render_manual_documents,render_manual_details]:
        for batch in controls:
            with catalog.engine.begin() as conn:
                conn.execute(text(renderer(fingerprint,catalog.schema,catalog.schema,**batch)))
    commands = [render_manual_credits(fingerprint,catalog.schema,catalog.schema,aliases_fingerprint=alias_fp,**batch) for batch in controls]
    assert all(statement.count(';') == 1 for statement in commands)
    with pytest.raises(DBAPIError,match='credit batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[1]))
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE normalization_aliases SET reason='Unreviewed change' WHERE alias_id=10"))
    with pytest.raises(DBAPIError,match='reviewed alias source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[0]))
    with catalog.engine.begin() as conn:
        conn.execute(text('UPDATE normalization_aliases SET reason=NULL WHERE alias_id=10'))
    for statement in [commands[0],commands[0],commands[1],commands[0],commands[1]]:
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.connect() as conn:
        actual = conn.execute(text('''SELECT c.publication_id,n.kind,n.raw_name,c.role,c.role_name,c.position,c.nested_position,
            c.entity_id,c.resolution FROM catalog_contributions c JOIN catalog_names n USING(name_id)
            ORDER BY c.publication_id,c.role COLLATE "C",c.position,c.nested_position''')).all()
        expected = []
        for identifier,payload in enumerate(payloads,1):
            for credit in decompose_metadata(payload)['credits']:
                entity,resolution = {'Known publisher':(1,'confirmed'),'Known person':(3,'unconfirmed')}.get(credit['raw_name'],(None,'unconfirmed'))
                expected.append((identifier,credit['kind'],credit['raw_name'],credit['role'],credit['role_name'],credit['position'],credit['nested_position'],entity,resolution))
        assert actual == expected
        assert conn.execute(text('SELECT count(*) FROM catalog_names')).scalar_one() == 5
        assert conn.execute(text("SELECT manifest->'manual_credit_progress' FROM catalog_imports")).scalar_one()['loaded_documents'] == 2
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one()[-1] == 'credits'
        assert conn.execute(text("SELECT nextval(pg_get_serial_sequence('catalog_names','name_id'))")).scalar_one() == 6
    assert snapshot_fingerprint(read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)) == fingerprint
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_contributions SET role_name='Untracked edit' WHERE publication_id=1 AND role='contributor'"))
    with pytest.raises(DBAPIError,match='credit mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[0]))


def test_editor_location_batches_preserve_partial_storage_and_verify_retries(staged_catalog):
    from scripts.catalog_manual_locations import render_manual_locations
    from scripts.catalog_manual_documents import document_source_signature_query
    from app.catalog.importer import snapshot_fingerprint
    from sqlalchemy.exc import DBAPIError
    import datetime

    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        conn.execute(text('DELETE FROM catalog_imports'))
        documents = [
            {'md5':'a'*32,'ya_path':"/Owner's; source.pdf",'ya_resource_id':'resource','ya_public_url':'https://example.test/public',
             'ya_public_key':'public-key','document_url':'s3://primary/book.pdf','content_url':'s3://content/book.txt',
             'primary_storage_size':42,'primary_storage_etag':'etag','primary_storage_verified_at':'2026-10-06T12:00:00+03:00'},
            {'md5':'b'*32,'ya_path':'','primary_storage_size':0,'primary_storage_etag':'partial'},
            {'md5':'c'*32,'primary_storage_verified_at':'2026-10-06T09:00:00+00:00'},
            {'md5':'d'*32},
        ]
        for identifier,doc in enumerate(documents,1):
            fields = ','.join(doc)
            values = ','.join(':'+key for key in doc)
            conn.execute(text(f'INSERT INTO document({fields}) VALUES({values})'),doc)
            conn.execute(catalog.table('publications').insert().values(publication_id=identifier,work_type='CreativeWork',has_metadata=False,metadata_present=False))
            conn.execute(catalog.table('documents').insert().values(md5=doc['md5'],publication_id=identifier,complete=False,restricted=True))
        conn.execute(text('''INSERT INTO catalog_imports(source_fingerprint,state,manifest)
            VALUES(:fingerprint,'loading',CAST(:manifest AS JSONB))'''),
            {'fingerprint':'0'*64,'manifest':json.dumps({'manual_editor':True,'evidence_mode':'essential',
                'dataset_schema':catalog.schema,'documents':4,
                'manual_completed_steps':['initialized','foundation','taxonomy','aliases','documents','metadata_details','credits'],
                'manual_credit_progress':{'loaded_documents':4,'loaded_credits':0,'loaded_names':0,'last_md5':'d'*32},
                'source_tables':{'document':{'rows':4},'metadata':{'rows':0},'library_collection_items':{'rows':0}}})})
    source = read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)
    controls = []
    with catalog.engine.connect() as conn:
        for lower,upper,offset,final in [(None,'b'*32,0,False),('b'*32,'d'*32,2,True)]:
            signatures = {name:conn.execute(text(document_source_signature_query(catalog.schema,name,lower,upper))).scalar_one()
                          for name in ('document','metadata','library_collection_items')}
            controls.append(dict(lower_md5=lower,upper_md5=upper,offset=offset,rows=2,final_batch=final,source_signatures=signatures))
    commands = [render_manual_locations('0'*64,catalog.schema,catalog.schema,**batch) for batch in controls]
    assert all(statement.count(';')==1 for statement in commands)
    with pytest.raises(DBAPIError,match='location batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[1]))
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE document SET primary_storage_etag='Unreviewed' WHERE md5=:md5"),{'md5':'b'*32})
    with pytest.raises(DBAPIError,match='document batch source changed'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[0]))
    with catalog.engine.begin() as conn:
        assert conn.execute(text('SELECT count(*) FROM catalog_locations')).scalar_one()==0
        conn.execute(text("UPDATE document SET primary_storage_etag='partial' WHERE md5=:md5"),{'md5':'b'*32})
    for statement in [commands[0],commands[0],commands[1],commands[0],commands[1]]:
        with catalog.engine.begin() as conn:
            conn.execute(text(statement))
    with catalog.engine.connect() as conn:
        rows = conn.execute(text('SELECT location_id,md5,provider,purpose,locator,source_path,resource_id,public_url,public_key,size,etag,verified_at FROM catalog_locations ORDER BY location_id')).all()
        timestamp = datetime.datetime(2026,10,6,9,tzinfo=datetime.timezone.utc)
        assert rows == [
            (1,'a'*32,'yandex','source',None,"/Owner's; source.pdf",'resource','https://example.test/public','public-key',None,None,None),
            (2,'a'*32,'s3','primary','s3://primary/book.pdf',None,None,None,None,42,'etag',timestamp),
            (3,'a'*32,'s3','content','s3://content/book.txt',None,None,None,None,None,None,None),
            (4,'b'*32,'yandex','source',None,'',None,None,None,None,None,None),
            (5,'b'*32,'s3','primary',None,None,None,None,None,0,'partial',None),
            (6,'c'*32,'s3','primary',None,None,None,None,None,None,None,timestamp)]
        assert conn.execute(text("SELECT manifest->'manual_location_progress' FROM catalog_imports")).scalar_one()=={'loaded_documents':4,'loaded_locations':6,'last_md5':'d'*32}
        assert conn.execute(text("SELECT manifest->'manual_completed_steps' FROM catalog_imports")).scalar_one()[-1]=='locations'
        assert conn.execute(text("SELECT nextval(pg_get_serial_sequence('catalog_locations','location_id'))")).scalar_one()==7
    assert snapshot_fingerprint(read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema))==snapshot_fingerprint(source)
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_locations SET public_url='Untracked' WHERE location_id=1"))
    with pytest.raises(DBAPIError,match='location mapping differs'):
        with catalog.engine.begin() as conn:
            conn.execute(text(commands[0]))


def test_editor_location_renderer_accepts_tripled_batch_and_rejects_larger():
    from scripts.catalog_manual_locations import render_manual_locations

    controls = dict(lower_md5=None,upper_md5='f'*32,offset=0,rows=7500,final_batch=False,
                    source_signatures={name:'0'*64 for name in ('document','metadata','library_collection_items')})
    statement = render_manual_locations('0'*64,**controls)
    assert statement.count(';')==1
    assert 'loaded<7500' in statement
    with pytest.raises(ValueError,match='7500 documents'):
        render_manual_locations('0'*64,**{**controls,'rows':7501})


def test_editor_storage_cleanup_preserves_rows_and_checks_indexes_in_readonly_service(staged_catalog):
    from scripts.catalog_manual_storage import render_performance_index_cleanup
    from app.catalog.importer import snapshot_fingerprint
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    with catalog.engine.begin() as conn:
        conn.execute(text("INSERT INTO document(md5) VALUES (repeat('a',32)),(repeat('b',32))"))
        conn.execute(text("""INSERT INTO library_collection_document_features
            (md5,input_hash,eligible,title_core,created_at,updated_at)
            VALUES (repeat('a',32),'hash',true,'First','now','now'),
                   (repeat('b',32),'hash',false,'Second','now','now')"""))
        fingerprint = conn.execute(text('SELECT source_fingerprint FROM catalog_imports')).scalar_one()
        conn.exec_driver_sql("""UPDATE catalog_imports SET state='loading',manifest=manifest ||
            '{"manual_editor":true,"evidence_mode":"essential"}'::jsonb""")
        conn.execute(text("""UPDATE catalog_imports SET manifest=jsonb_set(manifest,
            '{source_tables,library_collection_document_features,rows}','2')"""))
        definitions = conn.execute(text("""SELECT indexname,indexdef FROM pg_indexes
            WHERE schemaname=:schema AND indexname IN
            ('idx_library_collection_features_core_trgm','idx_library_collection_features_eligible_core')"""),
            {'schema':catalog.schema}).all()
        original_manifest = conn.execute(text('SELECT manifest FROM catalog_imports')).scalar_one()
    source = read_snapshot(catalog.engine, domain_schema=catalog.schema, dataset_schema=catalog.schema)
    statement = render_performance_index_cleanup(fingerprint, catalog.schema)
    assert statement.count(';') == 1
    with pytest.raises(ValueError, match='fingerprint'):
        render_performance_index_cleanup('invalid')
    # An editor that does not carry the preceding write-mode command must fail closed.
    with pytest.raises(DBAPIError, match='read-only transaction'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql('SET TRANSACTION READ ONLY')
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT manifest FROM catalog_imports').scalar_one() == original_manifest
        assert all(conn.execute(text('SELECT to_regclass(:index) IS NOT NULL'),
                               {'index':name}).scalar_one() for name,_ in definitions)
    with catalog.engine.begin() as conn:
        conn.execute(text('DROP INDEX idx_library_collection_features_eligible_core'))
        conn.execute(text('CREATE UNIQUE INDEX idx_library_collection_features_eligible_core '
                          'ON library_collection_document_features(eligible,title_core)'))
    with pytest.raises(DBAPIError, match='index definition changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT to_regclass('idx_library_collection_features_core_trgm') IS NOT NULL").scalar_one()
        assert conn.exec_driver_sql('SELECT manifest FROM catalog_imports').scalar_one() == original_manifest
        conn.exec_driver_sql('DROP INDEX idx_library_collection_features_eligible_core')
        conn.exec_driver_sql(dict(definitions)['idx_library_collection_features_eligible_core'])
    # Match the actual cleanup connection path, not a multi-statement editor session.
    with catalog.engine.connect() as conn:
        try:
            with conn.begin():
                conn.exec_driver_sql('SET default_transaction_read_only=on')
            for _ in range(2):
                with conn.begin():
                    conn.exec_driver_sql('SET TRANSACTION READ WRITE')
                    conn.exec_driver_sql(statement)
                with conn.begin():
                    assert conn.exec_driver_sql('SHOW transaction_read_only').scalar_one() == 'on'
            with conn.begin():
                saved = conn.exec_driver_sql("SELECT manifest->'manual_deferred_performance_indexes' FROM catalog_imports").scalar_one()
                assert len(saved) == 2
                assert {row['name']:row['definition'] for row in saved} == dict(definitions)
                assert conn.exec_driver_sql("SELECT to_regclass('library_collection_document_features_pkey') IS NOT NULL").scalar_one()
        finally:
            conn.rollback()
            with conn.begin():
                conn.exec_driver_sql('SET default_transaction_read_only=off')
    assert snapshot_fingerprint(read_snapshot(catalog.engine, domain_schema=catalog.schema,
                                            dataset_schema=catalog.schema)) == snapshot_fingerprint(source)


def test_additional_storage_cleanup_checks_progress_and_restores_exact_indexes(staged_catalog):
    from scripts.catalog_manual_storage import render_additional_performance_index_cleanup
    from app.catalog.importer import snapshot_fingerprint
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    names = ['idx_library_book_previews_status', 'idx_library_non_pdf_extraction_queue',
             'idx_library_metadata_quality_state_status', 'idx_catalog_contributions_name_role',
             'idx_catalog_contributions_publication', 'idx_catalog_contributions_entity',
             'idx_catalog_documents_publication', 'idx_catalog_preview_queue']
    previous = ['initialized','foundation','taxonomy','aliases','documents','metadata_details',
                'credits','locations','previews']
    remembered = {'schema':catalog.schema,'table':'library_collection_document_features',
                  'name':'previously_deferred','definition':'previous definition','bytes':123}
    with catalog.engine.begin() as conn:
        fingerprint = conn.exec_driver_sql('SELECT source_fingerprint FROM catalog_imports').scalar_one()
        conn.execute(text("""UPDATE catalog_imports SET state='loading',manifest=manifest || CAST(:extra AS jsonb)"""),
                     {'extra':json.dumps({'manual_editor':True,'evidence_mode':'essential',
                         'manual_completed_steps':previous,'manual_credit_progress':{'loaded_credits':0},
                         'manual_preview_progress':{'loaded_requests':0},
                         'manual_deferred_performance_indexes':[remembered]})})
        definitions = dict(conn.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=:schema AND indexname=ANY(:names)"),
                                        {'schema':catalog.schema,'names':names}).all())
        assert len(definitions)==8
        original_manifest=conn.exec_driver_sql('SELECT manifest FROM catalog_imports').scalar_one()
    source=read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)
    statement=render_additional_performance_index_cleanup(fingerprint,catalog.schema)
    assert statement.count(';')==1
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE catalog_imports SET manifest=jsonb_set(manifest,'{documents}','1')")
    with pytest.raises(DBAPIError,match='cleanup row count changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        conn.execute(text('UPDATE catalog_imports SET manifest=CAST(:manifest AS jsonb)'),
                     {'manifest':json.dumps(original_manifest)})
        conn.exec_driver_sql('DROP INDEX idx_catalog_preview_queue')
        conn.exec_driver_sql('CREATE UNIQUE INDEX idx_catalog_preview_queue ON catalog_preview_requests(status,request_id)')
    with pytest.raises(DBAPIError,match='index definition changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT to_regclass('idx_library_book_previews_status') IS NOT NULL").scalar_one()
        assert conn.exec_driver_sql('SELECT manifest FROM catalog_imports').scalar_one()==original_manifest
        conn.exec_driver_sql('DROP INDEX idx_catalog_preview_queue')
        conn.exec_driver_sql(definitions['idx_catalog_preview_queue'])
    with catalog.engine.connect() as conn:
        try:
            with conn.begin():
                conn.exec_driver_sql('SET default_transaction_read_only=on')
            for _ in range(2):
                with conn.begin():
                    conn.exec_driver_sql('SET TRANSACTION READ WRITE')
                    conn.exec_driver_sql(statement)
            with conn.begin():
                assert conn.exec_driver_sql('SHOW transaction_read_only').scalar_one()=='on'
                saved=conn.exec_driver_sql("SELECT manifest->'manual_deferred_performance_indexes' FROM catalog_imports").scalar_one()
                assert saved[0]==remembered and len(saved)==9
                assert {row['name']:row['definition'] for row in saved[1:]}==definitions
        finally:
            conn.rollback()
            with conn.begin():
                conn.exec_driver_sql('SET default_transaction_read_only=off')
    assert snapshot_fingerprint(read_snapshot(catalog.engine,domain_schema=catalog.schema,
                                            dataset_schema=catalog.schema))==snapshot_fingerprint(source)
    with catalog.engine.begin() as conn:
        for definition in definitions.values():
            conn.exec_driver_sql(definition)
        assert conn.exec_driver_sql('SELECT count(*) FROM pg_index WHERE indrelid=\'catalog_preview_requests\'::regclass AND indisunique').scalar_one()>=2


@pytest.mark.parametrize('staged_catalog', ['essential'], indirect=True)
def test_editor_identity_reviews_preserve_types_merges_separations_and_retry(staged_catalog):
    from scripts.catalog_manual_reviews import render_manual_reviews, review_signature_queries
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    previous=['initialized','foundation','taxonomy','aliases','documents','metadata_details',
              'credits','locations','previews','evidence']
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO normalization_canonicals(canonical_id,entity_type,display_name,
            normalized_name,status,notes,created_at,updated_at)
            VALUES(1,'publisher','Merged','merged','active','','now','now')""")
        conn.execute(catalog.table('entities').insert(),[
            {'entity_id':2,'kind':'person','display_name':'Survivor','approval':'unconfirmed','revision':4}])
        conn.execute(catalog.table('entities').insert().values(entity_id=1,kind='organization',
            display_name='Merged',approval='unconfirmed',status='merged',merged_into_id=2))
        conn.execute(catalog.table('names').insert(),[
            {'name_id':1,'kind':'person','raw_name':'Shared'}, {'name_id':2,'kind':'organization','raw_name':'Shared'}])
        conn.execute(catalog.table('publications').insert().values(publication_id=1))
        conn.execute(catalog.table('contributions').insert(),[
            {'contribution_id':1,'publication_id':1,'name_id':1,'role':'publisher','position':0},
            {'contribution_id':2,'publication_id':1,'name_id':2,'role':'author','position':0}])
        conn.execute(catalog.table('entity_roles').insert().values(entity_id=1,role='publisher'))
        conn.execute(catalog.table('aliases').insert().values(alias_id=1,name_id=2,entity_id=1))
        analysis=conn.exec_driver_sql("""INSERT INTO publisher_merge_analyses(fingerprint,scope,inventory,metadata,state,created_at,updated_at)
            VALUES('review','all','{}','{}','imported','now','now') RETURNING analysis_id""").scalar_one()
        for index,status in enumerate(('pending','skipped','applied'),1):
            proposal={'proposed_name':"Owner's; group",'member_ids':['raw:Shared','canonical:1','canonical:2',"raw:Owner's; New"]}
            conn.execute(text("""INSERT INTO publisher_merge_proposals(proposal_id,analysis_id,fingerprint,proposal,members,status,created_at,updated_at)
                VALUES(:id,:analysis,:fingerprint,CAST(:proposal AS jsonb),'[]',:status,'now','now')"""),
                {'id':index,'analysis':analysis,'fingerprint':str(index),'proposal':json.dumps(proposal),'status':status})
        conn.exec_driver_sql("""INSERT INTO normalization_suggestions(suggestion_id,entity_type,raw_name,normalized_name,
            target_canonical_id,suggestion_kind,confidence,confidence_band,status,created_at,updated_at)
            VALUES(1,'personality','Observed person','Preferred',1,'alias',0.9,'high','open','now','now')""")
        conn.execute(text("""INSERT INTO publisher_separations(left_key,right_key,provenance,created_at)
            VALUES(:left,'raw:Shared','{}','now')"""),{'left':"raw:Owner's; New"})
        fingerprint=conn.exec_driver_sql('SELECT source_fingerprint FROM catalog_imports').scalar_one()
        conn.execute(text("""UPDATE catalog_imports SET state='loading',manifest=manifest||CAST(:extra AS jsonb)"""),
            {'extra':json.dumps({'manual_editor':True,'evidence_mode':'essential','actor':'catalog-migration',
                'dataset_schema':catalog.schema,'manual_completed_steps':previous,
                'manual_credit_progress':{'loaded_names':2},'manual_evidence_progress':{'loaded_evidence':0,'total_evidence':0},
                'source_tables':{'normalization_suggestions':{'rows':1},'publisher_merge_proposals':{'rows':3},'publisher_separations':{'rows':1}}})})
        signatures={key:conn.exec_driver_sql(query).scalar_one() for key,query in
                    review_signature_queries(catalog.schema,base_name_max_id=2).items()}
    statement=render_manual_reviews(fingerprint,catalog.schema,catalog.schema,base_name_max_id=2,signatures=signatures)
    assert statement.count(';')==1
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE publisher_merge_proposals SET status='staged' WHERE proposal_id=1")
    with pytest.raises(DBAPIError,match='review source changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_proposals').scalar_one()==0
        conn.exec_driver_sql("UPDATE publisher_merge_proposals SET status='pending' WHERE proposal_id=1")
        conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert dict(conn.exec_driver_sql('SELECT status,count(*) FROM catalog_proposals GROUP BY status').all())=={'pending':2,'deferred':1}
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_proposal_members').scalar_one()==8
        assert conn.exec_driver_sql("SELECT count(*) FROM catalog_proposal_members WHERE entity_id=2 AND reviewed_revision=4").scalar_one()==3
        assert conn.exec_driver_sql("SELECT count(*) FROM catalog_proposal_members WHERE snapshot->>'raw_name'='Shared' AND snapshot->>'kind'='person'").scalar_one()==2
        assert conn.exec_driver_sql("SELECT count(*) FROM catalog_entities WHERE approval='confirmed'").scalar_one()==0
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_names').scalar_one()==4
        separation=conn.exec_driver_sql('SELECT left_key,right_key,actor FROM catalog_separations').one()
        assert separation.actor=='catalog-migration' and separation.left_key<separation.right_key
        assert conn.exec_driver_sql("SELECT manifest->'manual_completed_steps' FROM catalog_imports").scalar_one()[-1]=='reviews'
        assert conn.exec_driver_sql("SELECT nextval(pg_get_serial_sequence('catalog_proposals','proposal_id'))").scalar_one()==4
        conn.exec_driver_sql("UPDATE catalog_proposal_members SET snapshot='{}' WHERE member_id=1")
    with pytest.raises(DBAPIError,match='review mapping differs'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with pytest.raises(ValueError,match='base name'):
        render_manual_reviews(fingerprint,base_name_max_id=True,signatures=signatures)


@pytest.mark.parametrize('staged_catalog', ['essential'], indirect=True)
def test_editor_evidence_batches_preserve_exceptions_and_reject_changed_sources(staged_catalog):
    from scripts.catalog_manual_evidence import render_manual_evidence
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    previous = ['initialized','foundation','taxonomy','aliases','documents','metadata_details',
                'credits','locations','previews']
    md5='a'*32
    with catalog.engine.begin() as conn:
        conn.execute(text('INSERT INTO document(md5) VALUES(:md5)'),{'md5':md5})
        conn.execute(text("INSERT INTO classification(id,ddc,path_en,path_en_key) VALUES(1,'100','[\"Test\"]',:key)"),
                     {'key':"Owner's; test"})
        conn.execute(text("INSERT INTO metadata(md5,schema_org,classification_id) VALUES(:md5,'{}',1)"),{'md5':md5})
        conn.execute(catalog.table('publications').insert().values(publication_id=1))
        conn.execute(catalog.table('documents').insert().values(md5=md5,publication_id=1))
        conn.execute(text("""UPDATE catalog_imports SET state='loading',manifest=manifest||CAST(:extra AS jsonb)"""),
                     {'extra':json.dumps({'manual_editor':True,'evidence_mode':'essential',
                         'dataset_schema':catalog.schema,'manual_completed_steps':previous,
                         'source_tables':{'document':{'rows':1},'metadata':{'rows':1},'classification':{'rows':1}}})})
        records=[]
        for position,(table,key,payload) in enumerate([
                ('document',md5,{'full':None,'sharing_restricted':None}),
                ('classification','1',{'path_en':['Test'],'path_en_key':"Owner's; test",'path_tt':None})],1):
            column='id' if table=='classification' else 'md5'
            signature=conn.execute(text(f"SELECT encode(sha256(convert_to(to_jsonb(s)::text,'UTF8')),'hex') FROM {table} s WHERE {column}::text=:key"),{'key':key}).scalar_one()
            records.append({'evidence_id':position,'md5':None if table=='classification' else md5,
                'record_kind':'import','record_key':table+':0','source':'legacy.'+table,'payload':payload,
                'source_table':table,'source_key':key,'source_hash':signature})
        fingerprint=conn.exec_driver_sql('SELECT source_fingerprint FROM catalog_imports').scalar_one()
    def command(offset,rows):
        with catalog.engine.begin() as conn:
            signature=conn.execute(text("""SELECT encode(sha256(convert_to(string_agg(
                encode(sha256(convert_to(payload::text,'UTF8')),'hex'),'' ORDER BY evidence_id),'UTF8')),'hex')
                FROM jsonb_to_recordset(CAST(:records AS jsonb)) AS input(evidence_id bigint,payload jsonb)"""),
                {'records':json.dumps(rows)}).scalar_one()
        return render_manual_evidence(fingerprint,catalog.schema,catalog.schema,offset=offset,total=2,
                                      records=rows,payload_signature=signature)
    first=command(0,records[:1])
    last=command(1,records[1:])
    assert first.count(';')==last.count(';')==1
    wrong=command(0,[{**records[0],'payload':{'full':True}}])
    with pytest.raises(DBAPIError,match='payload mapping differs'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(wrong)
    with pytest.raises(DBAPIError,match='evidence batch is out of order'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(last)
    for statement in (first,first):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_evidence').scalar_one()==1
        conn.exec_driver_sql("UPDATE classification SET path_en_key='Changed' WHERE id=1")
    with pytest.raises(DBAPIError,match='evidence source changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(last)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_evidence').scalar_one()==1
        conn.execute(text("UPDATE classification SET path_en_key=:key WHERE id=1"),{'key':"Owner's; test"})
    for statement in (last,first,last):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT manifest->'manual_evidence_progress' FROM catalog_imports").scalar_one()=={'loaded_evidence':2,'total_evidence':2}
        assert conn.exec_driver_sql("SELECT manifest->'manual_completed_steps' FROM catalog_imports").scalar_one()[-1]=='evidence'
        assert conn.exec_driver_sql("SELECT payload->>'path_en_key' FROM catalog_evidence WHERE evidence_id=2").scalar_one()=="Owner's; test"
        conn.exec_driver_sql("UPDATE catalog_evidence SET payload='{}' WHERE evidence_id=1")
    with pytest.raises(DBAPIError,match='evidence mapping differs'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(first)
    for bad in (True,-1,1.5):
        with pytest.raises(ValueError,match='offset'):
            render_manual_evidence(fingerprint,offset=bad,total=2,records=records[:1],payload_signature='0'*64)
    with pytest.raises(ValueError,match='source'):
        render_manual_evidence(fingerprint,total=2,offset=0,records=[{**records[0],'source_table':'arbitrary'}],payload_signature='0'*64)


def test_editor_previews_preserve_public_pages_and_block_private_legacy_assets(staged_catalog):
    from scripts.catalog_manual_previews import preview_source_signature_query, render_manual_previews
    from app.catalog.importer import snapshot_fingerprint
    from sqlalchemy.exc import DBAPIError

    catalog, _ = staged_catalog
    previous = ['initialized','foundation','taxonomy','aliases','documents','metadata_details','credits','locations']
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql('DELETE FROM catalog_imports')
        for position,(letter,restricted,status,pages) in enumerate([
                ('a',False,'ready',(1,2,10)),('b',False,'processing',(1,None,4)),
                ('c',True,'ready',(1,None,7)),('d',None,'partial',(1,2,7))],1):
            md5 = letter*32
            conn.execute(text('INSERT INTO document(md5,mime_type,sharing_restricted) VALUES(:md5,\'application/pdf\',:restricted)'),
                         {'md5':md5,'restricted':restricted})
            conn.execute(catalog.table('publications').insert().values(publication_id=position,work_type='Book'))
            conn.execute(catalog.table('documents').insert().values(md5=md5,publication_id=position,
                mime_type='application/pdf',restricted=restricted is not False))
            conn.execute(text("""INSERT INTO library_book_previews(md5,recipe_version,status,source_page_count,
                first_preview_page,second_preview_page,last_preview_page,created_at,updated_at)
                VALUES(:md5,:recipe,:status,:count,:first,:second,:last,'before','before')"""),
                {'md5':md5,'recipe':"Owner's; recipe",'status':status,'count':pages[-1],
                 'first':pages[0],'second':pages[1],'last':pages[2]})
        conn.execute(text("""INSERT INTO catalog_imports(source_fingerprint,state,manifest)
            VALUES(:fingerprint,'loading',CAST(:manifest AS jsonb))"""),
            {'fingerprint':'0'*64,'manifest':json.dumps({'documents':4,'actor':'catalog-migration',
                'manual_editor':True,'evidence_mode':'essential','dataset_schema':catalog.schema,
                'manual_completed_steps':previous,
                'manual_credit_progress':{'loaded_documents':4,'loaded_credits':0,'loaded_names':0,'last_md5':'d'*32},
                'manual_location_progress':{'loaded_documents':4,'loaded_locations':0,'last_md5':'d'*32},
                'source_tables':{'document':{'rows':4},'library_book_previews':{'rows':4}}})})
    source = read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)
    with catalog.engine.connect() as conn:
        signatures = {name:conn.execute(text(preview_source_signature_query(catalog.schema,name))).scalar_one()
                      for name in ('document','library_book_previews')}
    statement = render_manual_previews('0'*64,catalog.schema,catalog.schema,source_signatures=signatures)
    assert statement.count(';') == 1
    for bad_steps in (None,previous[:-1]):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE catalog_imports SET manifest=manifest-'manual_completed_steps'")
            if bad_steps is not None:
                conn.execute(text("UPDATE catalog_imports SET manifest=jsonb_set(manifest,'{manual_completed_steps}',CAST(:steps AS jsonb))"),
                             {'steps':json.dumps(bad_steps)})
        with pytest.raises(DBAPIError,match='preview phase is out of order'):
            with catalog.engine.begin() as conn:
                conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE catalog_imports SET manifest=jsonb_set(manifest,'{manual_completed_steps}',CAST(:steps AS jsonb))"),
                     {'steps':json.dumps(previous)})
    with catalog.engine.begin() as conn:
        conn.execute(text("UPDATE library_book_previews SET error_text='Unreviewed change' WHERE md5=:md5"),{'md5':'b'*32})
    with pytest.raises(DBAPIError,match='preview source changed'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM catalog_preview_requests').scalar_one() == 0
        conn.exec_driver_sql('UPDATE library_book_previews SET error_text=NULL')
        conn.execute(text('UPDATE catalog_documents SET restricted=false WHERE md5=:md5'),{'md5':'c'*32})
    with pytest.raises(DBAPIError,match='preview privacy mapping differs'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.begin() as conn:
        conn.execute(text('UPDATE catalog_documents SET restricted=true WHERE md5=:md5'),{'md5':'c'*32})
    for _ in range(2):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)
    with catalog.engine.connect() as conn:
        requests = conn.exec_driver_sql('SELECT * FROM catalog_preview_requests ORDER BY request_id').mappings().all()
        assert [row['status'] for row in requests] == ['ready','pending','failed','failed']
        assert [row['request_id'] for row in requests] == [1,2,3,4]
        assert all(row['private'] is False and row['recipe']=="Owner's; recipe" for row in requests)
        assert all(row['claim_token'] is None and row['lease_until'] is None for row in requests)
        assert all(row['error']=='Legacy public assets require regeneration into private storage' for row in requests[2:])
        pages = conn.exec_driver_sql('SELECT request_id,role,page_number,small_key,large_key FROM catalog_preview_pages ORDER BY request_id,page_number').all()
        assert pages == [(1,'first',1,'a'*32+'/1s.webp','a'*32+'/1l.webp'),
                         (1,'second',2,'a'*32+'/2s.webp','a'*32+'/2l.webp'),
                         (1,'last',10,'a'*32+'/ls.webp','a'*32+'/ll.webp'),
                         (2,'first',1,'b'*32+'/1s.webp','b'*32+'/1l.webp'),
                         (2,'last',4,'b'*32+'/ls.webp','b'*32+'/ll.webp')]
        manifest = conn.exec_driver_sql('SELECT manifest FROM catalog_imports').scalar_one()
        assert manifest['manual_completed_steps'] == previous+['previews']
        assert manifest['manual_preview_progress'] == {'loaded_requests':4,'loaded_pages':5,'blocked_legacy_requests':2}
        assert conn.exec_driver_sql("SELECT nextval(pg_get_serial_sequence('catalog_preview_requests','request_id'))").scalar_one() == 5
    assert snapshot_fingerprint(read_snapshot(catalog.engine,domain_schema=catalog.schema,dataset_schema=catalog.schema)) == snapshot_fingerprint(source)
    with catalog.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE catalog_preview_pages SET large_key='Untracked' WHERE request_id=1 AND role='first'")
    with pytest.raises(DBAPIError,match='preview mapping differs'):
        with catalog.engine.begin() as conn:
            conn.exec_driver_sql(statement)


@pytest.mark.parametrize('controls',[
    {'fingerprint':'invalid'}, {'source_signatures':{}},
    {'source_signatures':{'document':'0'*64,'library_book_previews':True}},
])
def test_editor_preview_renderer_rejects_unreviewed_controls(controls):
    from scripts.catalog_manual_previews import render_manual_previews

    args={'fingerprint':'0'*64,'source_signatures':{'document':'0'*64,'library_book_previews':'0'*64}}
    with pytest.raises(ValueError):
        render_manual_previews(**{**args,**controls})
