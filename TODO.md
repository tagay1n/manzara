# TODO

## General

- [ ] Rename the database schema.
- [ ] Move `gec-annotations-filter`.
- [ ] Add upstream metadata to the database.
- [ ] Add a notification panel.
- [ ] Revisit task hierarchy and concurrency.
- [ ] Investigate and fix the intermittent `test_task_completion_not_blocked_by_open_stdout_fd` timeout.
- [ ] Normalize metadata so it is English-only.
- [ ] Remove buckets containing `upstream_meta` and `schema.org`; keep those values locally only.

## Non-PDF extraction roadmap

The most troublesome supported formats are:

| Format | Main problems | Priority |
| --- | --- | --- |
| Legacy DOC / RTF | LibreOffice timeouts, mixed inline images, and image-only scans requiring OCR | Highest |
| FB2 | Duplicated section titles and custom XML/image handling | High |
| EPUB | Nested images, broken internal XHTML links, and leaked source attributes | High |
| Markdown / text | Legacy Yandex images, external URL ownership, and CP866 mistaken for UTF-16 | Medium |
| DOCX / ODT | Generally extracted cleanly | Lower |

Largest unsupported groups observed:

- PowerPoint/PPTX: 20
- Executables: 13; probably not documents
- DjVu: 10; important for books and likely the best next format
- PDFs hidden behind incorrect MIME types: 6
- Spreadsheets: 4
- Compound or unknown OLE files: 3
- MOBI, MDB, ODP, SCR, and WMF: one each

Suggested order:

1. Add OCR handling for image-only DOC/RTF files.
2. Add DjVu extraction/OCR.
3. Retain stronger regression sampling for FB2 and EPUB.
4. Add PowerPoint and spreadsheet extraction later.
5. Continue treating MIME as a hint and trusting byte signatures.

FB2 and EPUB caused the most structural-content bugs; legacy DOC/RTF caused the most operational and completeness problems.
