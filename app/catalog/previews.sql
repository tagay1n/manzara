SELECT set_config('manzara.preview_command', %(payload)s::text, true),
       set_config('lock_timeout', %(lock_timeout)s, true);
DO $manzara_preview_command$
DECLARE
    command jsonb := current_setting('manzara.preview_command')::jsonb;
    action text := command->>'action';
    snapshot jsonb := command->'snapshot';
    document_key text := command->>'md5';
    recipe_key text := command->>'recipe';
    actor_name text := command->>'actor';
    retry boolean := (command->>'retry')::boolean;
    fresh jsonb;
    old_request jsonb;
    new_request jsonb;
    request_key bigint := (command->>'request_id')::bigint;
BEGIN
    -- Remote commands already own the matching session lock. This is also
    -- the transaction fence for failure finalization after remote work exits.
    IF NOT pg_try_advisory_xact_lock(hashtext(current_schema()),
            hashtext('catalog-document-storage:' || document_key)) THEN
        RAISE EXCEPTION USING ERRCODE='MZ002', MESSAGE='Preview document is busy; retry after the current operation';
    END IF;
    PERFORM md5 FROM catalog_documents WHERE md5=document_key FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='MZ003', MESSAGE='Preview document no longer exists';
    END IF;

    IF action<>'fail' THEN
        -- Lock source owners before checking their revisions and public access.
        PERFORM publication_id FROM catalog_publications
            WHERE publication_id=(snapshot->>'publication_id')::bigint FOR UPDATE;
        PERFORM location_id FROM catalog_locations
            WHERE location_id=(snapshot->>'location_id')::bigint FOR UPDATE;
        SELECT to_jsonb(source) INTO fresh FROM ({{source_query}}) source;
        IF fresh IS NULL
                OR fresh - ARRAY['request_id','request_status','lease_until','verified_at']
                    IS DISTINCT FROM snapshot - 'verified_at'
                OR (fresh->>'verified_at')::timestamptz
                    IS DISTINCT FROM (snapshot->>'verified_at')::timestamptz THEN
            RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Preview source or eligibility changed; resume with a fresh batch';
        END IF;
    END IF;

    IF action='claim' THEN
        SELECT to_jsonb(source) INTO fresh FROM ({{pending_query}}) source;
        IF fresh IS NULL THEN
            PERFORM set_config('manzara.preview_result','null',true);
            RETURN;
        END IF;
        IF fresh->>'request_status' IN ('pending','processing') THEN
            request_key := (fresh->>'request_id')::bigint;
        ELSE
            INSERT INTO catalog_preview_requests(md5,recipe,actor,private,idempotency_key)
                VALUES(document_key,recipe_key,actor_name,false,command->>'idempotency_key')
                RETURNING request_id INTO request_key;
        END IF;
        UPDATE catalog_preview_requests SET status='processing',private=false,
            claim_token=command->>'claim_token',
            lease_until=clock_timestamp()+make_interval(secs => (command->>'lease_seconds')::integer)
            WHERE request_id=request_key AND (status='pending'
                OR (status='processing' AND lease_until<=clock_timestamp()))
            RETURNING to_jsonb(catalog_preview_requests) INTO new_request;
    ELSE
        SELECT to_jsonb(r) INTO old_request FROM catalog_preview_requests r
            WHERE r.request_id=request_key AND r.md5=document_key AND r.recipe=recipe_key FOR UPDATE;
        IF old_request IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='MZ003', MESSAGE='Preview request no longer exists';
        END IF;
        IF old_request->>'status'<>'processing'
                OR old_request->>'claim_token' IS DISTINCT FROM command->>'claim_token'
                OR (old_request->>'lease_until')::timestamptz IS NULL
                OR (old_request->>'lease_until')::timestamptz<=clock_timestamp() THEN
            RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Preview claim expired or was replaced';
        END IF;
        IF action='renew' THEN
            UPDATE catalog_preview_requests SET
                lease_until=clock_timestamp()+make_interval(secs => (command->>'lease_seconds')::integer)
                WHERE request_id=request_key RETURNING to_jsonb(catalog_preview_requests) INTO new_request;
        ELSIF action IN ('finish','fail') THEN
            IF action='finish' THEN
                IF (old_request->>'private')::boolean IS DISTINCT FROM false THEN
                    RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Preview request access changed; public save rolled back';
                END IF;
                INSERT INTO catalog_preview_pages(request_id,role,page_number,small_key,large_key)
                    SELECT request_key,role,page_number,small_key,large_key
                    FROM jsonb_to_recordset(command->'pages')
                        AS page(role text,page_number integer,small_key text,large_key text);
            END IF;
            UPDATE catalog_preview_requests SET status=CASE WHEN action='fail' THEN 'failed' ELSE 'ready' END,
                lease_until=NULL,source_page_count=(command->>'source_page_count')::integer
                WHERE request_id=request_key RETURNING to_jsonb(catalog_preview_requests) INTO new_request;
        ELSE
            RAISE EXCEPTION USING ERRCODE='MZ001', MESSAGE='Unsupported preview command';
        END IF;
    END IF;
    PERFORM set_config('manzara.preview_result',COALESCE(new_request,'null'::jsonb)::text,true);
END;
$manzara_preview_command$;
SELECT current_setting('manzara.preview_result')::jsonb;
