# Library collections

- Detection, Gemini validation, and explicit metadata application belong to the Collections flow. Reruns create resumable proposals/verdicts and never mutate canonical state.
- Eligibility requires object-valued `metadata.schema_org` with a usable title. Exclude legislation and normalized legal genres before indexing.
- Paths, filenames, directories, and storage hierarchy are never evidence or prompt input.
- Match approved signatures before grouping coherent unmatched records. A document has at most one canonical collection; conflicts require owner resolution.
- Only explicit owner approval creates collections or memberships. New collections need two approved documents; existing-collection attachments may contain one. Keep apply separate from proposal approval.
- Validation uses the shared model pool with one verdict per batch and no consensus voting. Start at or below 20; retry timeout/malformed output twice, then reduce `20 -> 10 -> 5 -> 2 -> 1`. `400`, `429`, blackout, and `5xx` do not change size.
- Validate response MD5 sets exactly; missing, duplicated, unknown, or malformed results fail the response.
