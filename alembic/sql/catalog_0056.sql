-- Frozen schema for revision 20261005_0056. Future changes need new migrations.

CREATE TABLE "__CATALOG_SCHEMA__".catalog_classification_nodes (
	node_id BIGSERIAL NOT NULL,
	ddc TEXT NOT NULL,
	parent_id BIGINT,
	label_en TEXT NOT NULL,
	label_tt TEXT,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (node_id),
	FOREIGN KEY(parent_id) REFERENCES "__CATALOG_SCHEMA__".catalog_classification_nodes (node_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_collections (
	collection_id BIGSERIAL NOT NULL,
	title TEXT NOT NULL,
	notes TEXT,
	include_in_library BOOLEAN DEFAULT 'true' NOT NULL,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (collection_id)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_entities (
	entity_id BIGSERIAL NOT NULL,
	kind TEXT NOT NULL,
	display_name TEXT NOT NULL,
	approval TEXT DEFAULT 'unconfirmed' NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	merged_into_id BIGINT,
	surname_full TEXT,
	surname_initials TEXT,
	name_full TEXT,
	name_initials TEXT,
	father_name_full TEXT,
	father_name_initials TEXT,
	title TEXT,
	sex TEXT,
	identity_key TEXT,
	notes TEXT,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (entity_id),
	CHECK (kind IN ('person','organization','unknown')),
	CHECK (approval IN ('unconfirmed','confirmed')),
	CHECK (status IN ('active','merged','archived')),
	FOREIGN KEY(merged_into_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_evidence (
	evidence_id BIGSERIAL NOT NULL,
	md5 TEXT,
	record_kind TEXT NOT NULL,
	record_key TEXT NOT NULL,
	source TEXT NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (evidence_id)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_imports (
	import_id BIGSERIAL NOT NULL,
	source_fingerprint TEXT NOT NULL,
	manifest JSONB NOT NULL,
	state TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (import_id),
	UNIQUE (source_fingerprint)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_names (
	name_id BIGSERIAL NOT NULL,
	kind TEXT NOT NULL,
	raw_name TEXT NOT NULL,
	PRIMARY KEY (name_id),
	UNIQUE (kind, raw_name)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_protections (
	record_kind TEXT NOT NULL,
	record_key TEXT NOT NULL,
	field TEXT NOT NULL,
	actor TEXT NOT NULL,
	PRIMARY KEY (record_kind, record_key, field)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_revisions (
	revision_id BIGSERIAL NOT NULL,
	record_kind TEXT NOT NULL,
	record_key TEXT NOT NULL,
	actor TEXT NOT NULL,
	before JSONB,
	after JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (revision_id)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_separations (
	left_key TEXT NOT NULL,
	right_key TEXT NOT NULL,
	actor TEXT NOT NULL,
	PRIMARY KEY (left_key, right_key),
	CHECK (left_key < right_key)
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_aliases (
	alias_id BIGSERIAL NOT NULL,
	name_id BIGINT NOT NULL,
	entity_id BIGINT NOT NULL,
	approval TEXT DEFAULT 'unconfirmed' NOT NULL,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (alias_id),
	UNIQUE (name_id, entity_id),
	FOREIGN KEY(name_id) REFERENCES "__CATALOG_SCHEMA__".catalog_names (name_id) ON DELETE RESTRICT,
	FOREIGN KEY(entity_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_classifications (
	classification_id BIGSERIAL NOT NULL,
	node_id BIGINT NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (classification_id),
	UNIQUE (node_id),
	FOREIGN KEY(node_id) REFERENCES "__CATALOG_SCHEMA__".catalog_classification_nodes (node_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_entity_roles (
	entity_id BIGINT NOT NULL,
	role TEXT NOT NULL,
	PRIMARY KEY (entity_id, role),
	FOREIGN KEY(entity_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_publications (
	publication_id BIGSERIAL NOT NULL,
	name TEXT,
	work_type TEXT,
	description TEXT,
	edition TEXT,
	date_published TEXT,
	page_count INTEGER,
	languages TEXT[] DEFAULT '{}' NOT NULL,
	audience_array BOOLEAN DEFAULT 'false' NOT NULL,
	inclusion TEXT DEFAULT 'pending' NOT NULL,
	evaluation_method TEXT,
	classification_id BIGINT,
	collection_id BIGINT,
	merged_into_id BIGINT,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (publication_id),
	CHECK (inclusion IN ('pending','included','excluded')),
	CHECK (page_count IS NULL OR page_count > 0),
	FOREIGN KEY(classification_id) REFERENCES "__CATALOG_SCHEMA__".catalog_classifications (classification_id) ON DELETE RESTRICT,
	FOREIGN KEY(collection_id) REFERENCES "__CATALOG_SCHEMA__".catalog_collections (collection_id) ON DELETE RESTRICT,
	FOREIGN KEY(merged_into_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_audiences (
	publication_id BIGINT NOT NULL,
	kind TEXT NOT NULL,
	audience_type TEXT,
	min_age INTEGER,
	max_age INTEGER,
	position INTEGER NOT NULL,
	PRIMARY KEY (publication_id, position),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_contributions (
	contribution_id BIGSERIAL NOT NULL,
	publication_id BIGINT NOT NULL,
	name_id BIGINT NOT NULL,
	entity_id BIGINT,
	role TEXT NOT NULL,
	role_name TEXT,
	position INTEGER NOT NULL,
	nested_position INTEGER DEFAULT '0' NOT NULL,
	resolution TEXT DEFAULT 'unconfirmed' NOT NULL,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (contribution_id),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE,
	FOREIGN KEY(name_id) REFERENCES "__CATALOG_SCHEMA__".catalog_names (name_id) ON DELETE RESTRICT,
	FOREIGN KEY(entity_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_documents (
	md5 TEXT NOT NULL,
	publication_id BIGINT NOT NULL,
	mime_type TEXT,
	complete BOOLEAN DEFAULT 'true' NOT NULL,
	restricted BOOLEAN DEFAULT 'false' NOT NULL,
	selected BOOLEAN DEFAULT 'true' NOT NULL,
	content_extraction_method TEXT,
	meta_extraction_method TEXT,
	access_modes TEXT[] DEFAULT '{}' NOT NULL,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (md5),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_genres (
	publication_id BIGINT NOT NULL,
	value TEXT NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (publication_id, position),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_identifiers (
	publication_id BIGINT NOT NULL,
	kind TEXT NOT NULL,
	value TEXT NOT NULL,
	normalized TEXT NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (publication_id, kind, position),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_proposals (
	proposal_id BIGSERIAL NOT NULL,
	kind TEXT NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	display_name TEXT,
	evidence JSONB DEFAULT '{}' NOT NULL,
	field_changes JSONB DEFAULT '{}' NOT NULL,
	publication_id BIGINT,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (proposal_id),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_reference_authors (
	publication_id BIGINT NOT NULL,
	kind TEXT,
	name TEXT,
	position INTEGER NOT NULL,
	PRIMARY KEY (publication_id, position),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_references (
	publication_id BIGINT NOT NULL,
	work_type TEXT,
	name TEXT,
	language TEXT,
	urls TEXT[],
	PRIMARY KEY (publication_id),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_subjects (
	publication_id BIGINT NOT NULL,
	name TEXT,
	term_code TEXT,
	set_name TEXT,
	set_url TEXT,
	set_is_url BOOLEAN NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (publication_id, position),
	FOREIGN KEY(publication_id) REFERENCES "__CATALOG_SCHEMA__".catalog_publications (publication_id) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_locations (
	location_id BIGSERIAL NOT NULL,
	md5 TEXT NOT NULL,
	provider TEXT NOT NULL,
	purpose TEXT NOT NULL,
	locator TEXT,
	source_path TEXT,
	resource_id TEXT,
	public_url TEXT,
	public_key TEXT,
	size BIGINT,
	etag TEXT,
	verified_at TIMESTAMP WITH TIME ZONE,
	revision BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (location_id),
	UNIQUE (md5, provider, purpose),
	CHECK (size IS NULL OR size >= 0),
	FOREIGN KEY(md5) REFERENCES "__CATALOG_SCHEMA__".catalog_documents (md5) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_preview_requests (
	request_id BIGSERIAL NOT NULL,
	md5 TEXT NOT NULL,
	idempotency_key TEXT NOT NULL,
	recipe TEXT NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	private BOOLEAN NOT NULL,
	actor TEXT NOT NULL,
	claim_token TEXT,
	lease_until TIMESTAMP WITH TIME ZONE,
	error TEXT,
	source_page_count INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (request_id),
	UNIQUE (md5, idempotency_key),
	FOREIGN KEY(md5) REFERENCES "__CATALOG_SCHEMA__".catalog_documents (md5) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_proposal_members (
	member_id BIGSERIAL NOT NULL,
	proposal_id BIGINT NOT NULL,
	entity_id BIGINT,
	name_id BIGINT,
	reviewed_revision BIGINT,
	snapshot JSONB NOT NULL,
	PRIMARY KEY (member_id),
	CHECK ((entity_id IS NULL) <> (name_id IS NULL)),
	FOREIGN KEY(proposal_id) REFERENCES "__CATALOG_SCHEMA__".catalog_proposals (proposal_id) ON DELETE CASCADE,
	FOREIGN KEY(entity_id) REFERENCES "__CATALOG_SCHEMA__".catalog_entities (entity_id) ON DELETE RESTRICT,
	FOREIGN KEY(name_id) REFERENCES "__CATALOG_SCHEMA__".catalog_names (name_id) ON DELETE RESTRICT
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_sufficient_modes (
	md5 TEXT NOT NULL,
	modes TEXT[] NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (md5, position),
	FOREIGN KEY(md5) REFERENCES "__CATALOG_SCHEMA__".catalog_documents (md5) ON DELETE CASCADE
);

CREATE TABLE "__CATALOG_SCHEMA__".catalog_preview_pages (
	request_id BIGINT NOT NULL,
	role TEXT NOT NULL,
	page_number INTEGER NOT NULL,
	small_key TEXT,
	large_key TEXT,
	PRIMARY KEY (request_id, role),
	FOREIGN KEY(request_id) REFERENCES "__CATALOG_SCHEMA__".catalog_preview_requests (request_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_catalog_branch ON "__CATALOG_SCHEMA__".catalog_classification_nodes (parent_id, label_en) WHERE parent_id IS NOT NULL;

CREATE UNIQUE INDEX uq_catalog_root ON "__CATALOG_SCHEMA__".catalog_classification_nodes (ddc, label_en) WHERE parent_id IS NULL;

CREATE INDEX idx_catalog_contributions_entity ON "__CATALOG_SCHEMA__".catalog_contributions (entity_id);

CREATE INDEX idx_catalog_contributions_publication ON "__CATALOG_SCHEMA__".catalog_contributions (publication_id);

CREATE INDEX idx_catalog_documents_publication ON "__CATALOG_SCHEMA__".catalog_documents (publication_id);

CREATE INDEX idx_catalog_preview_queue ON "__CATALOG_SCHEMA__".catalog_preview_requests (status, request_id);
