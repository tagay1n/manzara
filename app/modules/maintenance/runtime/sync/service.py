"""
Document Synchronization and Management Module

This module handles synchronization between Yandex.Disk storage and database, manages document 
filtering, and handles deduplication based on various criteria. It provides comprehensive 
document management functionality including content verification, duplicate detection, and 
automated file organization.

Key Features:
1. Document Synchronization
   - Syncs files between Yandex.Disk and database
   - Handles metadata updates and file publishing
   - Manages document visibility and access control

2. Document Filtering
   - Identifies non-Tatar language documents
   - Filters out non-textual content types
   - Handles document deduplication by ISBN
   - Manages restricted content separately

3. Storage Management
   - Handles S3 storage cleanup
   - Manages file movement between directories
   - Updates public sharing links

Constants:
    tatar_bcp_47_codes: List of BCP-47 language codes for Tatar variants
    not_document_types: List of MIME types to be filtered out

Key Functions:
    sync(): Main synchronization process
    _define_docs_for_wiping(): Identifies documents to be removed/moved
    _dedup_by_isbn(): Handles ISBN-based deduplication
    _process_file(): Processes individual files during sync
    _move_to_filtered_out(): Moves files to appropriate filtered directories

Process Flow:
1. Initial Setup
   - Load configuration
   - Connect to S3 and database
   - Retrieve upstream metadata

2. Document Processing
   - Identify documents for removal
   - Process each file in Yandex.Disk
   - Handle duplicates and invalid content
   - Update database records

3. Cleanup
   - Remove filtered content from S3
   - Move files to appropriate directories
   - Update wiping plan

Requirements:
- Yandex.Disk OAuth token
- S3 credentials and bucket configuration
- Database access
- Local storage for wiping plan

Error Handling:
- Graceful handling of API failures
- Transaction safety for database updates
- State persistence for interrupted operations
"""

from core.config import read_config
from core.yadisk import walk_yadisk, download_file_locally
from core.security import encrypt
from core.db import get_session
from integrations.yadisk import YaDisk
from rich import print
from models import Document
from integrations.s3 import create_session
from sqlalchemy import select
from metadata.fields import extract_isbn_values, parse_meta
import os
from collections import defaultdict
import pymupdf
import typer
from rich.console import Console
from rich.table import Table
from yadisk.exceptions import PathNotFoundError
from .constants import TATAR_BCP_47_CODES, NOT_DOCUMENT_TYPES
from .plan import flush, get_wiping_plan
from .repository import (
    delete_isbn_keep_many,
    get_all_md5s,
    list_docs_with_schema_org,
    load_isbn_keep_many_map,
    lookup_upstream_metadata,
    replace_isbn_keep_many,
)
from .rules import normalize_isbn, should_be_skipped
from .storage import move_to_filtered_out, publish_file, remove_from_s3


def sync():
    """
    Syncs files from Yandex Disk to Google Sheets.
    """
    config = read_config()
    s3client = create_session(config)

    with YaDisk(config['yandex']['disk']['oauth_token'], proxy=config['proxy']) as yaclient: 
        print("Requesting all upstream metadata urls") 
        upstream_metas = lookup_upstream_metadata(s3client, config)
        print("Requesting all md5s") 
        all_md5s = get_all_md5s(Document)
        
        print("Defining docs for wiping") 
        docs_for_wiping = _define_docs_for_wiping(yaclient, config) 
        if docs_for_wiping:
            print("Removing objects from s3 storage")
            remove_from_s3(docs_for_wiping.keys(), s3client, config)
        else:
            print("No docs for wiping found")
            
        print("Syncing yadisk with Google sheets")
        skipped = []
        for lang_tag, entry_point in config['yandex']['disk']['entry_points'].items():
            print(f"Processing entry point '{entry_point}' for language tag '{lang_tag}'")
            for file in walk_yadisk(client=yaclient, root=entry_point):
                try:
                    if dir_to_move := docs_for_wiping.get(file.md5, None):
                        # the file marked for wiping
                        move_to_filtered_out(file, config, yaclient, dir_to_move, entry_point)
                        # delete record in database
                        with get_session() as session:
                            if doc := session.get(Document, file.md5):
                                session.delete(doc)
                                session.commit()
                        del docs_for_wiping[file.md5]
                        flush(docs_for_wiping)
                    else:
                        meta = upstream_metas.get(file.md5)
                        if doc := _process_file(
                            yaclient, file, all_md5s,
                            skipped, meta, config, lang_tag, entry_point
                        ):
                            with get_session() as session:
                                session.merge(doc)
                                session.commit()

                except Exception as e:
                    import traceback
                    print(f"[red]Error during syncing: {type(e).__name__}: {e} {traceback.format_exc()}[/red]")
            if skipped:
                print("Skipped by MIME type files:")
                print(*skipped, sep="\n")
            
def _define_docs_for_wiping(yaclient, config):
    """Build and persist a plan of documents to move/remove."""
    docs_for_wiping = get_wiping_plan()

    print("Querying non tatar documents")
    with get_session() as session:
        non_tatar_docs = session.scalars(select(Document).where(Document.language.not_in(TATAR_BCP_47_CODES)))
        non_tatar_docs = {d.md5: f"nontatar/{'-'.join(sorted(d.language.split(', ')))}" for d in non_tatar_docs}
        print(f"Found {len(non_tatar_docs)} nontatar docs")
        docs_for_wiping.update(non_tatar_docs)
        flush(docs_for_wiping)
        
        print("Querying non textual docs")
        nontextual_docs = session.scalars(select(Document).where(
            Document.mime_type.in_(NOT_DOCUMENT_TYPES)
            | 
            Document.ya_path.endswith('.eaf') 
            |
            Document.ya_path.endswith('.musx')
        ))
        nontextual_docs = {d.md5: "nontextual" for d in nontextual_docs}
        print(f"Found {len(nontextual_docs)} nontextual docs")
        docs_for_wiping.update(nontextual_docs)
        flush(docs_for_wiping)
    
    _dedup_by_isbn(docs_for_wiping, yaclient, config)
    
    return docs_for_wiping
    
def _dedup_by_isbn(plan, yaclient, config):
    """Identify duplicate ISBNs and move extra copies to filtered-out."""
    print("Deduplicating by ISBN")
    # Get all docs that have metadata with potential ISBNs.
    with get_session() as session:
        docs = list_docs_with_schema_org(session)
        keep_many_map = load_isbn_keep_many_map(session)
    
    # Group them by ISBN
    md5s_to_docs = {}
    isbns_to_docs = defaultdict(set)
    for doc in docs:
        schema_org = doc.metadata_row.schema_org if getattr(doc, "metadata_row", None) else getattr(doc, "meta", None)
        isbns = sorted(
            {
                _isbn
                for isbn in extract_isbn_values(parse_meta(schema_org))
                if (_isbn := normalize_isbn(isbn))
            }
        )
        if not isbns:
            continue
        md5s_to_docs[doc.md5] = doc
        isbns = ", ".join(isbns)
        isbns_to_docs[isbns].add(doc.md5)
            
    # Find duplicates
    duplicated_isbn_to_md5s = defaultdict(set)
    duplicated_docs_md5s = set()
    for isbn, md5s in isbns_to_docs.items():
        if len(md5s) > 1 and isbn:
            if _is_isbn_group_marked_keep_many(isbn, md5s, keep_many_map):
                print(f"Skipping duplicate ISBN '{isbn}' (keep-many decision already stored)")
                continue
            print(f"Found duplicate ISBN: '{isbn}' with md5s {md5s}")
            duplicated_isbn_to_md5s[isbn].update(md5s)
            duplicated_docs_md5s.update(md5s)
    del isbns_to_docs
        
    if not duplicated_isbn_to_md5s:
        print("No duplicate ISBNs found, exiting...")
        return
    
    print(f"Downloading books with {len(duplicated_docs_md5s)} duplicate ISBNs")
    md5_to_local_path, unavailable_md5s = _download_duplicate_docs(
        duplicated_docs_md5s,
        md5s_to_docs,
        yaclient,
        config,
    )
    del duplicated_docs_md5s
        
    console = Console()
    for isbn, md5s in duplicated_isbn_to_md5s.items():
        if skipped_md5s := sorted(md5s & unavailable_md5s):
            print(
                f"[yellow]Skipping unavailable duplicate ISBN candidates for '{isbn}': "
                f"{', '.join(skipped_md5s)}[/yellow]"
            )
        docs_same_isbn = {md5s_to_docs[md5] for md5 in md5s if md5 in md5_to_local_path}
        if len(docs_same_isbn) < 2:
            print(
                f"[yellow]Skipping ISBN '{isbn}': only {len(docs_same_isbn)} downloadable "
                "docs remain after filtering missing public resources[/yellow]"
            )
            continue
        
        def _define_docs_to_move(_docs):
            _full_docs = set([d for d in _docs if d.full == True])
            # if we have only one full document among duplicates then keep it and move anothers
            if len(_full_docs) == 1:
                return _docs - _full_docs, False
            _pdf_docs = set([d for d in _docs if d.mime_type in ['application/pdf', 'application/x-pdf'] and d.full == True])
            # if we have exactly one full pdf among duplicates then keep it and move anothers
            if len(_pdf_docs) == 1:
                return _docs - _pdf_docs, False
            _extracted_pdf_docs = set([d for d in _pdf_docs if d.content_url])
            #  if we have multiple pdf docs, but only one of them already extracted then keep it and move anothers
            if len(_extracted_pdf_docs) == 1:
                return _docs - _extracted_pdf_docs, False
            
            _choices = {idx: doc for idx, doc in enumerate(sorted(_docs, key=lambda d: d.ya_public_url), start=1)}
            # _hint = []
            table = Table(title=isbn, expand=True, show_lines=True, show_header=False)
            table.add_column("#", justify="center", style="cyan", no_wrap=True)
            table.add_column(" ", )
            _params = set()
            for idx, doc in _choices.items():
                local_path = md5_to_local_path[doc.md5]
                if doc.mime_type in ['application/pdf', 'application/x-pdf']:
                    with pymupdf.open(local_path) as pdf_doc:
                        pages_count = str(pdf_doc.page_count)
                else:
                    pages_count = "N/A"
                size = round(os.path.getsize(local_path) / 1024 / 1024, 2)
                table.add_row(
                    str(idx),
                    f"md5: {doc.md5}\nlocal_path: {local_path}\nya_path: {doc.ya_path}\nsize: {size}\npages_count: {pages_count}\nfull: {doc.full}\nmime_type: {doc.mime_type}\ncontent_url: {doc.content_url if doc.content_url else 'N/A'}",
                )
                _params.add(f"{pages_count}-{size}-{doc.mime_type.strip()}-{doc.full}")
            if len(_params) == 1:
                # all files have same size and pages count, just pick the first
                return _docs - {_choices[1]}, False
            else:
                # ask user to choose which document to keep
                console.print(table)
                res = typer.prompt(
                    f"Multiple documents with ISBN '{isbn}' found, choose which one to keep "
                    "(or type 'all' to keep all docs for this ISBN)",
                    prompt_suffix="> ",
                )
                if res.strip().lower() in {"all", "a"}:
                    return set(), True
                if res.isdigit() and int(res) in _choices:
                    return _docs - {_choices[int(res)]}, False
                else:
                    print(f"Invalid choice '{res}', skipping ISBN {isbn}")
                    return None, False

        docs_for_wiping, keep_many = _define_docs_to_move(docs_same_isbn)
        if docs_for_wiping is None:
            continue
        with get_session() as session:
            if keep_many:
                replace_isbn_keep_many(session, isbn, {doc.md5 for doc in docs_same_isbn})
                print(f"Stored keep-many decision for ISBN '{isbn}'")
            else:
                delete_isbn_keep_many(session, isbn)
        if docs_for_wiping:
            plan.update({d.md5: f"duplicated_isbn/{isbn}" for d in docs_for_wiping})
            flush(plan)


def _download_duplicate_docs(duplicated_docs_md5s, md5s_to_docs, yaclient, config):
    """Download duplicate candidates and skip records whose public resource no longer exists."""
    md5_to_local_path = {}
    unavailable_md5s = set()
    for doc_md5 in duplicated_docs_md5s:
        doc = md5s_to_docs[doc_md5]
        try:
            md5_to_local_path[doc_md5] = download_file_locally(yaclient, doc, config)
        except PathNotFoundError as exc:
            unavailable_md5s.add(doc_md5)
            print(
                f"[yellow]Skipping duplicate ISBN candidate {doc_md5}: "
                f"public resource not found for {doc.ya_path} ({exc})[/yellow]"
            )
    return md5_to_local_path, unavailable_md5s


def _is_isbn_group_marked_keep_many(isbn_key: str, md5s: set[str], keep_many_map: dict[str, set[str]]) -> bool:
    """Return True when current ISBN group is already covered by a stored keep-many decision."""
    allowed_md5s = keep_many_map.get(isbn_key)
    return bool(allowed_md5s) and md5s.issubset(allowed_md5s)


def _is_limited_doc_path(path: str) -> bool:
    """Return True when the Yandex path points to a known limited milli_kitaphana folder."""
    normalized_path = path.casefold()
    return (
        "/limited/" in normalized_path
        and (
            "/milli_kitaphana/" in normalized_path
            or "/милли.китапханә/" in normalized_path
        )
    )


def _process_file(ya_client, file, all_md5s, skipped_by_mime_type_files, upstream_meta, config, lang_tag, entry_point):
    """Process a single Yandex Disk file and return a Document to upsert."""
    if file.path.startswith("disk:/neurotatarlar/kitaplar/monocorpus/Anna's archive/") and file.path.endswith('.txt'):
        print(f"Skipping Anna's archive file '{file.path}'")
        return
    if '/neurotatarlar/kitaplar/monocorpus/_1st_priority_for_OCR/random_files_thru_yandex_search/ilbyak-school.narod.ru' in file.path and file.path.endswith('.htm'):
        print(f"Skipping ilbyak-school.narod.ru file '{file.path}'")
        return
    
    _should_be_skipped, mime_type = should_be_skipped(file)
    if _should_be_skipped:
        move_to_filtered_out(file, config, ya_client, 'nontextual', entry_point)
        skipped_by_mime_type_files.append((file.mime_type, file.public_url, file.path))
        return
    
    ya_public_key = file.public_key
    ya_public_url = file.public_url
    if not (ya_public_key and ya_public_url):
        ya_public_key, ya_public_url = publish_file(ya_client, file.path)
    
    ya_path = file.path.removeprefix('disk:')    
    if file.md5 in all_md5s:
        # compare with ya_resource_id
        # if 'resource_id' is the same, then skip, due to we have it in gsheet
        # if not, then remove from yadisk due to it is duplicate
        if all_md5s[file.md5]['resource_id'] != file.resource_id:
            print(f"File '{file.path}' already exists in gsheet, but with different resource_id: '{file.resource_id}' with md5 '{file.md5}', removing it from yadisk")
            ya_client.remove(file.path, md5=file.md5)
            return
        # if md5 is the same but path or ya_public_url is different, proceed to updating
        if (all_md5s[file.md5]['ya_path'] == ya_path):
            return
        
    print(f"[green]Adding file to gsheets '{file.path}' with md5 '{file.md5}'[/green]")

    sharing_restricted = config["yandex"]["disk"]["hidden"] in file.path 
    doc = Document()
    doc.md5=file.md5
    doc.mime_type=mime_type
    doc.ya_path=ya_path
    doc.ya_public_key=ya_public_key
    doc.ya_public_url=encrypt(ya_public_url, config) if sharing_restricted else ya_public_url
    doc.sharing_restricted=sharing_restricted
    doc.ya_resource_id=file.resource_id
    doc.upstream_meta_url=upstream_meta
    doc.full = not _is_limited_doc_path(file.path)
    # update gsheet
    all_md5s[file.md5] = {"resource_id": doc.ya_resource_id, "upstream_meta_url": doc.upstream_meta_url} 
    return doc
