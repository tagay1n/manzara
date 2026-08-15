# Shayan flow guidance

These rules apply to `app/modules/shayan/`.

## State and artifacts

- Download manifests and snapshot history are PostgreSQL-backed. Persistent `status.json` or `latest.json` files are never runtime truth.
- Shayan structured results follow the shared event-first artifact contract: compact live counters via SSE and detailed changes through PostgreSQL/API.

## Hetzner video archive

- `shayan.upload_yadisk` is a stable historical task ID whose visible task is `Upload`; it sends newly downloaded local videos directly to the configured `nextcloud.shayan.<category>.target_dir`. It must not call Yandex Disk.
- Direct uploads keep PostgreSQL checkpoints and delete the local source only after the final Hetzner object is independently verified.
- Use Nextcloud chunked upload v2 for video-sized files. Assemble at a deterministic temporary DAV path, independently stream-hash uploaded bytes, then move to the final path.
- Staging components are short and MD5-based: `.manzara-<md5>.uploading`. Do not use `.part`; Hetzner Storage Share returns a server-side `500 TypeError` when probing those paths.
- Emit bounded chunk byte progress using `task.progress`, never log parsing.
- Persist ETag/checksum checkpoints and reuse verified final or temporary uploads after restart.
- Run one logged WebDAV preflight before discovery. Authentication failures stop once with actionable context. Rate limits retry the same request with interruptible backoff; read-only probes retry transient server errors at the same safe boundary.
