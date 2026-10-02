"""PostgreSQL publisher analysis checkpoints and durable review operations."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations

from app.repositories.core import utc_now

EMPTY_DRAFT = {"renames": [], "keeps": [], "merges": []}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _draft(conn):
    conn.execute(
        "INSERT INTO publisher_review_draft(singleton,revision,changes,reviewed,proposal_ids,updated_at) VALUES (1,0,?::jsonb,'{}'::jsonb,'[]'::jsonb,?) ON CONFLICT DO NOTHING",
        (_json(EMPTY_DRAFT), utc_now()),
    )
    return conn.execute(
        "SELECT * FROM publisher_review_draft WHERE singleton=1 FOR UPDATE"
    ).fetchone()


def _identity(conn, key):
    if key.startswith("canonical:"):
        cid = int(key.removeprefix("canonical:"))
        row = conn.execute(
            "SELECT canonical_id,display_name,status FROM normalization_canonicals WHERE entity_type='publisher' AND canonical_id=? FOR UPDATE",
            (cid,),
        ).fetchone()
        if not row or row["status"] != "active":
            raise ValueError("Publisher changed; refresh the review before applying.")
        aliases = conn.execute(
            "SELECT raw_name FROM normalization_aliases WHERE entity_type='publisher' AND canonical_id=? AND decision_status='linked' ORDER BY raw_name FOR UPDATE",
            (cid,),
        ).fetchall()
        return {
            "display_name": row["display_name"],
            "aliases": sorted(
                {row["display_name"], *(alias["raw_name"] for alias in aliases)}
            ),
            "is_new": False,
        }
    if not key.startswith("raw:") or not key[4:]:
        raise ValueError("Invalid publisher key")
    alias = conn.execute(
        "SELECT canonical_id, decision_status FROM normalization_aliases WHERE entity_type='publisher' AND raw_name=? FOR UPDATE",
        (key[4:],),
    ).fetchone()
    if alias and alias["decision_status"] == "linked":
        raise ValueError("Publisher alias changed; refresh the review before applying.")
    return {"display_name": key[4:], "aliases": [], "is_new": True}


def _keys(changes):
    result = {f"canonical:{item['canonical_id']}" for item in changes["renames"]}
    result.update(f"raw:{name}" for name in changes["keeps"])
    for merge in changes["merges"]:
        result.update(f"canonical:{cid}" for cid in merge["canonical_ids"])
        result.update(f"raw:{name}" for name in merge["raw_names"])
    return sorted(result)


class PublisherMergeRepository:
    def list_publisher_source_documents(self):
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT md5,schema_org FROM metadata WHERE lib IS TRUE AND schema_org IS NOT NULL ORDER BY md5"
                ).fetchall()
            ]

    def publisher_analysis_lock(self):
        """Reserve one session, leaving another for short catalog transactions."""
        if self.pool_size < 2:
            raise ValueError(
                "Publisher analysis requires a database pool of at least two connections."
            )
        from contextlib import contextmanager

        @contextmanager
        def locked():
            with self._connect() as conn:
                acquired = conn.execute(
                    "SELECT pg_try_advisory_lock(726193450) AS acquired"
                ).fetchone()["acquired"]
                if not acquired:
                    raise ValueError("A publisher analysis is already active.")
                # Session advisory locks survive COMMIT. Avoid an idle transaction
                # throughout the potentially hour-long Codex analysis.
                conn.execute("COMMIT")
                try:
                    yield
                finally:
                    conn.execute("SELECT pg_advisory_unlock(726193450)")

        return locked()

    def publisher_analysis_state(self):
        with self._connect() as conn:
            success = conn.execute(
                "SELECT 1 FROM publisher_merge_analyses WHERE state IN ('checkpointed','imported') LIMIT 1"
            ).fetchone()
            checkpoint = conn.execute(
                "SELECT * FROM publisher_merge_analyses WHERE state='checkpointed' ORDER BY analysis_id LIMIT 1"
            ).fetchone()
            separations = conn.execute(
                "SELECT left_key,right_key FROM publisher_separations ORDER BY left_key,right_key"
            ).fetchall()
        return {
            "successful": bool(success),
            "checkpoint": dict(checkpoint) if checkpoint else None,
            "separations": [[row["left_key"], row["right_key"]] for row in separations],
        }

    def get_publisher_analysis(self, analysis_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM publisher_merge_analyses WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        if not row:
            raise ValueError("Publisher analysis does not exist")
        return dict(row)

    def create_publisher_analysis(self, inventory, fingerprint, scope, metadata):
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE publisher_merge_analyses SET state='failed',updated_at=? WHERE state='generating'",
                (now,),
            )
            cursor = conn.execute(
                "INSERT INTO publisher_merge_analyses(fingerprint,scope,inventory,metadata,state,created_at,updated_at) VALUES (?,?,?::jsonb,?::jsonb,'generating',?,?)",
                (fingerprint, scope, _json(inventory), _json(metadata), now, now),
            )
            return int(cursor.lastrowid)

    def checkpoint_publisher_analysis(self, analysis_id, groups, metadata):
        with self._connect() as conn:
            conn.execute(
                "UPDATE publisher_merge_analyses SET response=?::jsonb,metadata=?::jsonb,state='checkpointed',updated_at=? WHERE analysis_id=? AND state='generating'",
                (_json(groups), _json(metadata), utc_now(), analysis_id),
            )

    def reject_publisher_checkpoint(self, analysis_id, reason):
        with self._connect() as conn:
            conn.execute(
                "UPDATE publisher_merge_analyses SET state='failed',metadata=metadata || ?::jsonb,updated_at=? WHERE analysis_id=? AND state='checkpointed'",
                (_json({"failure_context": reason}), utc_now(), analysis_id),
            )

    def import_publisher_analysis(self, analysis_id):
        now = utc_now()
        with self._connect() as conn:
            _draft(conn)
            row = conn.execute(
                "SELECT * FROM publisher_merge_analyses WHERE analysis_id=? FOR UPDATE",
                (analysis_id,),
            ).fetchone()
            if not row or row["state"] not in ("checkpointed", "imported"):
                raise ValueError("No completed publisher response checkpoint")
            if row["state"] == "imported":
                return 0
            pairs = conn.execute(
                "SELECT left_key,right_key FROM publisher_separations"
            ).fetchall()
            for group in row["response"]:
                if any(
                    {pair["left_key"], pair["right_key"]}.issubset(group["member_ids"])
                    for pair in pairs
                ):
                    raise ValueError(
                        "Completed response conflicts with a new separation decision; refresh and review that decision."
                    )
            inventory = {entry["key"]: entry for entry in row["inventory"]}
            count = 0
            for group in row["response"]:
                members = [inventory[key] for key in sorted(group["member_ids"])]
                identities = [
                    {
                        key: member[key]
                        for key in ("key", "display_name", "aliases", "is_new")
                    }
                    for member in members
                ]
                fingerprint = hashlib.sha256(_json(identities).encode()).hexdigest()
                # Pending owner edits live in the draft and are never replaced on reruns.
                existing = conn.execute(
                    "SELECT 1 FROM publisher_merge_proposals WHERE fingerprint=? AND status IN ('pending','staged','skipped')",
                    (fingerprint,),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO publisher_merge_proposals(analysis_id,fingerprint,proposal,members,created_at,updated_at) VALUES (?,?,?::jsonb,?::jsonb,?,?)",
                    (analysis_id, fingerprint, _json(group), _json(members), now, now),
                )
                count += 1
            conn.execute(
                "UPDATE publisher_merge_analyses SET state='imported',updated_at=? WHERE analysis_id=?",
                (now, analysis_id),
            )
            return count

    def discard_publisher_analyses(self):
        """Explicit owner reset of generated analyses; preserve domain decisions."""
        with self._lock, self._connect() as conn:
            draft = _draft(conn)
            staged = conn.execute(
                "SELECT proposal,review_edit FROM publisher_merge_proposals WHERE status='staged'"
            ).fetchall()
            owned = {
                key
                for row in staged
                for key in (row["review_edit"] or row["proposal"])["member_ids"]
            }
            changes = draft["changes"]
            changes["merges"] = [
                merge
                for merge in changes["merges"]
                if not owned.intersection(
                    _keys({"renames": [], "keeps": [], "merges": [merge]})
                )
            ]
            changes["keeps"] = [
                name for name in changes["keeps"] if f"raw:{name}" not in owned
            ]
            changes["renames"] = [
                item
                for item in changes["renames"]
                if f"canonical:{item['canonical_id']}" not in owned
            ]
            reviewed = {
                key: value
                for key, value in draft["reviewed"].items()
                if key in _keys(changes)
            }
            conn.execute(
                "UPDATE publisher_review_draft SET changes=?::jsonb,reviewed=?::jsonb,proposal_ids='[]'::jsonb,revision=revision+1,updated_at=? WHERE singleton=1",
                (_json(changes), _json(reviewed), utc_now()),
            )
            conn.execute("DELETE FROM publisher_merge_proposals")
            conn.execute("DELETE FROM publisher_merge_analyses")

    def get_publisher_review(self):
        with self._connect() as conn:
            draft = _draft(conn)
            rows = conn.execute(
                "SELECT proposal_id,proposal,members,status,review_edit FROM publisher_merge_proposals WHERE status IN ('pending','staged','skipped') ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'skipped' THEN 1 ELSE 2 END, CASE proposal->>'confidence' WHEN 'strong' THEN 0 WHEN 'possible' THEN 1 ELSE 2 END, proposal_id"
            ).fetchall()
            analysis = conn.execute(
                "SELECT inventory,response FROM publisher_merge_analyses WHERE state='imported' ORDER BY analysis_id DESC LIMIT 1"
            ).fetchone()
        proposals = [dict(row) for row in rows]
        member_proposals = {}
        for item in proposals:
            members = (item["review_edit"] or item["proposal"])["member_ids"]
            for key in members:
                member_proposals.setdefault(key, set()).add(item["proposal_id"])
        for item in proposals:
            members = (item["review_edit"] or item["proposal"])["member_ids"]
            overlaps = [key for key in members if len(member_proposals[key]) > 1]
            name = (item["review_edit"] or {}).get(
                "display_name", item["proposal"]["proposed_name"]
            )
            selected = [row for row in item["members"] if row["key"] in members]
            item["proposed_aliases"] = sorted(
                {
                    variant
                    for row in selected
                    for variant in [row["display_name"], *row["aliases"]]
                }
                - {name}
            )
            item["conflict_member_ids"] = overlaps
            item["conflicting_proposal_ids"] = sorted(
                set().union(*(member_proposals[key] for key in overlaps))
                - {item["proposal_id"]}
            )
        return {
            "coverage": {
                "inventory_entries": len(analysis["inventory"]) if analysis else 0,
                "covered_entries": len(
                    {
                        key
                        for cluster in analysis["response"]
                        for key in cluster["member_ids"]
                    }
                )
                if analysis
                else 0,
                "cluster_count": sum(
                    cluster["kind"] == "cluster" for cluster in analysis["response"]
                )
                if analysis
                else 0,
                "singleton_entries": sum(
                    cluster["kind"] == "singleton" for cluster in analysis["response"]
                )
                if analysis
                else 0,
                "unresolved_entries": sum(
                    cluster["kind"] == "unresolved" for cluster in analysis["response"]
                )
                if analysis
                else 0,
            },
            "revision": draft["revision"],
            "draft": draft["changes"],
            "proposal_ids": draft["proposal_ids"],
            "proposals": sorted(
                proposals, key=lambda item: not item["conflict_member_ids"]
            ),
        }

    def save_publisher_draft(self, changes, revision, expected=None):
        with self._lock, self._connect() as conn:
            draft = _draft(conn)
            if draft["revision"] != revision:
                raise ValueError("Publisher draft conflict; refresh the page.")
            reviewed = {}
            for key in _keys(changes):
                current = _identity(conn, key)
                snapshot = (
                    draft["reviewed"].get(key) or (expected or {}).get(key) or current
                )
                if (
                    expected is not None
                    and key not in draft["reviewed"]
                    and key not in expected
                ):
                    raise ValueError("Missing reviewed publisher; refresh the page.")
                if snapshot != current:
                    raise ValueError(
                        "Publisher changed since review; refresh the page."
                    )
                reviewed[key] = snapshot
            # Preserve staged groups if a manual caller saves its remaining changes.
            for pid in draft["proposal_ids"]:
                proposal = conn.execute(
                    "SELECT proposal FROM publisher_merge_proposals WHERE proposal_id=?",
                    (pid,),
                ).fetchone()["proposal"]
                owned = set(proposal["member_ids"])
                if len(owned) == 1 and not owned.issubset(_keys(changes)):
                    raise ValueError(
                        "Draft contains staged clusters; apply or discard before removing them."
                    )
                old_merges = [
                    merge
                    for merge in draft["changes"]["merges"]
                    if set(
                        _keys({"renames": [], "keeps": [], "merges": [merge]})
                    ).issubset(proposal["member_ids"])
                ]
                for old in old_merges:
                    members = set(_keys({"renames": [], "keeps": [], "merges": [old]}))
                    if not any(
                        set(_keys({"renames": [], "keeps": [], "merges": [new]}))
                        == members
                        for new in changes["merges"]
                    ):
                        raise ValueError(
                            "Draft contains staged suggestions; discard or apply them before removing changes."
                        )
            conn.execute(
                "UPDATE publisher_review_draft SET changes=?::jsonb,reviewed=?::jsonb,revision=revision+1,updated_at=? WHERE singleton=1",
                (_json(changes), _json(reviewed), utc_now()),
            )
        return self.get_publisher_review()

    def discard_publisher_draft(self, revision):
        with self._lock, self._connect() as conn:
            draft = _draft(conn)
            if draft["revision"] != revision:
                raise ValueError("Publisher draft conflict; refresh the page.")
            conn.execute(
                "UPDATE publisher_merge_proposals SET review_edit=NULL,updated_at=? WHERE status IN ('pending','staged','skipped')",
                (utc_now(),),
            )
            for pid in draft["proposal_ids"]:
                conn.execute(
                    "UPDATE publisher_merge_proposals SET status='pending',updated_at=? WHERE proposal_id=? AND status='staged'",
                    (utc_now(), pid),
                )
            conn.execute(
                "UPDATE publisher_review_draft SET changes=?::jsonb,reviewed='{}'::jsonb,proposal_ids='[]'::jsonb,revision=revision+1,updated_at=? WHERE singleton=1",
                (_json(EMPTY_DRAFT), utc_now()),
            )
        return self.get_publisher_review()

    def publisher_review_action(self, proposal_id, action, member_ids, display_name):
        now = utc_now()
        with self._lock, self._connect() as conn:
            draft = _draft(conn)
            row = conn.execute(
                "SELECT * FROM publisher_merge_proposals WHERE proposal_id=? FOR UPDATE",
                (proposal_id,),
            ).fetchone()
            if not row or row["status"] not in ("pending", "skipped"):
                raise ValueError("Proposal is no longer available; refresh the review.")
            entries = {member["key"]: member for member in row["members"]}
            if not set(member_ids).issubset(entries):
                raise ValueError("Unknown proposal member")
            members = [entries[key] for key in member_ids]
            if action == "edit":
                conn.execute(
                    "UPDATE publisher_merge_proposals SET review_edit=?::jsonb,updated_at=? WHERE proposal_id=?",
                    (
                        _json({"member_ids": member_ids, "display_name": display_name}),
                        now,
                        proposal_id,
                    ),
                )
                status = row["status"]
            elif action == "stage":
                changes = draft["changes"]
                if set(member_ids).intersection(_keys(changes)):
                    raise ValueError(
                        "Publisher is already staged; apply or discard the draft first."
                    )
                reviewed = draft["reviewed"]
                for key, member in zip(member_ids, members):
                    expected = {
                        field: member[field]
                        for field in ("display_name", "aliases", "is_new")
                    }
                    if _identity(conn, key) != expected:
                        raise ValueError(
                            "Publisher proposal is stale; refresh and run another analysis."
                        )
                    reviewed[key] = expected
                if len(members) == 1:
                    member = members[0]
                    if member["is_new"]:
                        if display_name != member["display_name"]:
                            raise ValueError(
                                "Preserve the singleton name; rename it after applying."
                            )
                        changes["keeps"].append(member["raw_name"])
                    elif display_name != member["display_name"]:
                        changes["renames"].append(
                            {
                                "canonical_id": member["canonical_id"],
                                "display_name": display_name,
                            }
                        )
                    else:
                        raise ValueError(
                            "This singleton is already an approved publisher."
                        )
                else:
                    changes["merges"].append(
                        {
                            "canonical_ids": [
                                member["canonical_id"]
                                for member in members
                                if not member["is_new"]
                            ],
                            "raw_names": [
                                member["raw_name"]
                                for member in members
                                if member["is_new"]
                            ],
                            "display_name": display_name,
                        }
                    )
                conn.execute(
                    "UPDATE publisher_review_draft SET changes=?::jsonb,reviewed=?::jsonb,proposal_ids=?::jsonb,revision=revision+1,updated_at=? WHERE singleton=1",
                    (
                        _json(changes),
                        _json(reviewed),
                        _json([*draft["proposal_ids"], proposal_id]),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE publisher_merge_proposals SET review_edit=?::jsonb WHERE proposal_id=?",
                    (
                        _json({"member_ids": member_ids, "display_name": display_name}),
                        proposal_id,
                    ),
                )
                status = "staged"
            elif action == "separate":
                for key in member_ids:
                    _identity(conn, key)
                for left, right in combinations(sorted(member_ids), 2):
                    conn.execute(
                        "INSERT INTO publisher_separations(left_key,right_key,provenance,created_at) VALUES (?,?,?::jsonb,?) ON CONFLICT DO NOTHING",
                        (
                            left,
                            right,
                            _json({"proposal_id": proposal_id, "members": members}),
                            now,
                        ),
                    )
                status = "separate"
            else:
                chosen_name = (
                    display_name
                    or (row["review_edit"] or {}).get("display_name")
                    or row["proposal"]["proposed_name"]
                )
                if (
                    member_ids != row["proposal"]["member_ids"]
                    or chosen_name != row["proposal"]["proposed_name"]
                    or row["review_edit"]
                ):
                    conn.execute(
                        "UPDATE publisher_merge_proposals SET review_edit=?::jsonb WHERE proposal_id=?",
                        (
                            _json(
                                {"member_ids": member_ids, "display_name": chosen_name}
                            ),
                            proposal_id,
                        ),
                    )
                status = "skipped"
            conn.execute(
                "UPDATE publisher_merge_proposals SET status=?,updated_at=? WHERE proposal_id=?",
                (status, now, proposal_id),
            )
        return self.get_publisher_review()

    def _prepare_publisher_draft_apply(self, conn, revision):
        draft = _draft(conn)
        if revision != draft["revision"]:
            raise ValueError("Publisher draft conflict; refresh the page.")
        for key, expected in draft["reviewed"].items():
            if _identity(conn, key) != expected:
                raise ValueError(
                    "Publisher changed since review; refresh before applying."
                )
        return draft

    def _finish_publisher_draft_apply(self, conn, draft):
        for pid in draft["proposal_ids"]:
            conn.execute(
                "UPDATE publisher_merge_proposals SET status='applied',updated_at=? WHERE proposal_id=? AND status='staged'",
                (utc_now(), pid),
            )
        conn.execute(
            "UPDATE publisher_review_draft SET changes=?::jsonb,reviewed='{}'::jsonb,proposal_ids='[]'::jsonb,revision=revision+1,updated_at=? WHERE singleton=1",
            (_json(EMPTY_DRAFT), utc_now()),
        )

    def _reconcile_publisher_separations(self, conn, mapping):
        rows = conn.execute(
            "SELECT * FROM publisher_separations ORDER BY left_key,right_key FOR UPDATE"
        ).fetchall()
        for row in rows:
            left, right = row["left_key"], row["right_key"]
            mapped = sorted({mapping.get(left, left), mapping.get(right, right)})
            if mapped == [left, right]:
                continue
            conn.execute(
                "DELETE FROM publisher_separations WHERE left_key=? AND right_key=?",
                (left, right),
            )
            if len(mapped) == 2:
                conn.execute(
                    "INSERT INTO publisher_separations(left_key,right_key,provenance,created_at) VALUES (?,?,?::jsonb,?) ON CONFLICT DO NOTHING",
                    (*mapped, _json(row["provenance"]), row["created_at"]),
                )
