"""Normalization suggestion generation and persistence."""

from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional


from app.db import Database
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelResponseError,
    run_ordered_model_pool,
)
from app.gemini_runtime import GeminiRuntimeManager
from app.modules.library.normalization import (
    _canonical_name_map,
    _confidence_band,
    _entity_config,
    _similarity,
    get_review_queue,
)

def _parse_first_json_blob(value: str) -> Optional[Dict[str, Any]]:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text_value)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _gemini_suggest(
    *,
    entity_type: str,
    raw_name: str,
    normalized_name: str,
    docs_count: int,
    mentions_count: int,
    marker_count: int,
    canonical_candidates: List[Dict[str, Any]],
    manager: GeminiRuntimeManager,
) -> Optional[Dict[str, Any]]:
    try:
        from google import genai

        candidates_block = "\n".join(
            [
                f"- id={item['canonical_id']} name={item['display_name']} normalized={item['normalized_name']} aliases={item['linked_aliases']}"
                for item in canonical_candidates[:10]
            ]
        )
        prompt = (
            "You are normalizing bibliographic entities. "
            "Return strict JSON with keys: suggestion_kind(link|create|reject), target_canonical_id(number or null), confidence(0..1), rationale(string).\n"
            f"Entity type: {entity_type}\n"
            f"Alias raw name: {raw_name}\n"
            f"Alias normalized: {normalized_name}\n"
            f"Docs: {docs_count}, Mentions: {mentions_count}, MarkerCount: {marker_count}\n"
            "Canonical candidates:\n"
            f"{candidates_block if candidates_block else '- none'}\n"
            "Rules: choose link only when semantically same, otherwise create or reject."
        )

        def parse_response(response: Any) -> Dict[str, Any]:
            raw_text = getattr(response, "text", None)
            if not raw_text and hasattr(response, "candidates"):
                candidates = getattr(response, "candidates", None) or []
            else:
                candidates = []
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) or []
                for part in parts:
                    text_part = getattr(part, "text", None)
                    if text_part:
                        raw_text = str(text_part)
                        break
                if raw_text:
                    break
            parsed = _parse_first_json_blob(raw_text or "")
            if not parsed:
                raise GeminiModelResponseError("response is not a JSON object")
            kind = str(parsed.get("suggestion_kind") or "").strip().lower()
            if kind not in {"link", "create", "reject"}:
                raise GeminiModelResponseError("invalid suggestion_kind")
            confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
            target = parsed.get("target_canonical_id")
            target_id = int(target) if isinstance(target, (int, float, str)) and str(target).strip() else None
            if kind != "link":
                target_id = None
            return {
                "suggestion_kind": kind,
                "target_canonical_id": target_id,
                "confidence": confidence,
                "confidence_band": _confidence_band(confidence),
                "rationale": str(parsed.get("rationale") or ""),
            }

        result = run_ordered_model_pool(
            manager=manager,
            models=load_required_gemini_model_pool(),
            request=lambda model, api_key, _lease: genai.Client(api_key=api_key).models.generate_content(
                model=model, contents=prompt
            ),
            parse=parse_response,
            record_failure=lambda *_args: None,
            run_id=None,
        )
        return {**result.value, "model": result.model_name}
    except GeminiModelPoolExhaustedError:
        return None
    except Exception:
        raise


def _heuristic_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int,
    use_gemini: bool,
    manager: Optional[GeminiRuntimeManager] = None,
    workers: int = 1,
) -> List[Dict[str, Any]]:
    canonicals = db.list_normalization_canonicals(entity_type)
    queue = get_review_queue(
        db,
        entity_type,
        status="all",
        page=1,
        page_size=10000,
    )
    items = queue.get("items") or []
    unresolved = [
        item
        for item in items
        if str(item.get("queue_status") or "") in {"unreviewed", "suggested"}
    ]
    unresolved.sort(key=lambda item: (-int(item.get("docs_count") or 0), -int(item.get("mentions_count") or 0)))

    suggestions: List[Dict[str, Any]] = []
    gemini_budget = 20
    gemini_jobs: List[tuple[int, Future[Optional[Dict[str, Any]]]]] = []
    executor = (
        ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="normalization-worker")
        if use_gemini and manager is not None
        else None
    )

    for item in unresolved[: max(1, int(limit))]:
        raw_name = str(item.get("raw_name") or "")
        normalized_name = str(item.get("normalized_name") or "")
        docs_count = int(item.get("docs_count") or 0)
        mentions_count = int(item.get("mentions_count") or 0)
        marker_count = int(item.get("marker_count") or 0)

        best_canonical: Optional[Dict[str, Any]] = None
        best_score = 0.0
        ranked: List[Dict[str, Any]] = []
        for canonical in canonicals:
            score = _similarity(normalized_name, canonical.get("normalized_name"))
            if score <= 0.0:
                continue
            ranked.append({"canonical": canonical, "score": score})
            if score > best_score:
                best_score = score
                best_canonical = canonical
        ranked.sort(key=lambda row: -float(row.get("score") or 0.0))

        if best_canonical and best_score >= 0.92:
            kind = "link"
            confidence = max(0.92, min(0.99, best_score))
            target_id = int(best_canonical.get("canonical_id") or 0)
            rationale = "Exact or near-exact normalized match"
            model = "heuristic"
        elif best_canonical and best_score >= 0.8:
            kind = "link"
            confidence = max(0.78, min(0.9, best_score))
            target_id = int(best_canonical.get("canonical_id") or 0)
            rationale = "High lexical similarity to canonical"
            model = "heuristic"
        elif docs_count >= 2:
            kind = "create"
            confidence = 0.66
            target_id = None
            rationale = "Frequent unresolved alias should become canonical candidate"
            model = "heuristic"
        else:
            kind = "reject"
            confidence = 0.52
            target_id = None
            rationale = "Low-evidence alias, likely noise or formatting variant"
            model = "heuristic"

        gemini_future = None
        if use_gemini and gemini_budget > 0 and manager is not None and executor is not None:
            gemini_future = executor.submit(
                _gemini_suggest,
                entity_type=entity_type,
                raw_name=raw_name,
                normalized_name=normalized_name,
                docs_count=docs_count,
                mentions_count=mentions_count,
                marker_count=marker_count,
                canonical_candidates=[
                    {
                        "canonical_id": int(row["canonical"].get("canonical_id") or 0),
                        "display_name": str(row["canonical"].get("display_name") or ""),
                        "normalized_name": str(row["canonical"].get("normalized_name") or ""),
                        "linked_aliases": int(row["canonical"].get("linked_aliases") or 0),
                    }
                    for row in ranked[:10]
                ],
                manager=manager,
            )
            gemini_budget -= 1

        suggestion = {
            "raw_name": raw_name,
            "normalized_name": normalized_name,
            "target_canonical_id": int(target_id) if target_id else None,
            "suggestion_kind": kind,
            "confidence": round(float(confidence), 3),
            "confidence_band": _confidence_band(float(confidence)),
            "model": model,
            "rationale": rationale,
        }
        suggestions.append(suggestion)
        if gemini_future is not None:
            gemini_jobs.append((len(suggestions) - 1, gemini_future))

    try:
        for index, future in gemini_jobs:
            gemini_pick = future.result()
            if not gemini_pick:
                continue
            suggestion = suggestions[index]
            suggestion["suggestion_kind"] = str(
                gemini_pick.get("suggestion_kind") or suggestion["suggestion_kind"]
            )
            target = gemini_pick.get("target_canonical_id")
            suggestion["target_canonical_id"] = int(target) if target else None
            confidence = float(gemini_pick.get("confidence") or suggestion["confidence"])
            suggestion["confidence"] = round(confidence, 3)
            suggestion["confidence_band"] = _confidence_band(confidence)
            suggestion["rationale"] = str(
                gemini_pick.get("rationale") or suggestion["rationale"]
            )
            suggestion["model"] = str(gemini_pick.get("model") or "gemini")
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    return suggestions


def refresh_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int = 120,
    use_gemini: bool = True,
    workers: int = 1,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Regenerate open suggestion set from unresolved queue."""
    _entity_config(entity_type)
    limit = max(1, min(1000, int(limit)))

    manager: Optional[GeminiRuntimeManager] = None
    if bool(use_gemini):
        manager = GeminiRuntimeManager(
            db,
            task_id=f"library.{entity_type}_suggestions_refresh",
            panel_id="library",
            should_stop=should_stop,
        )

    suggestions = _heuristic_suggestions(
        db,
        entity_type,
        limit=limit,
        use_gemini=bool(use_gemini),
        manager=manager,
        workers=workers,
    )
    db.replace_open_suggestions(entity_type, suggestions)

    counts = {"high": 0, "medium": 0, "low": 0}
    for item in suggestions:
        band = str(item.get("confidence_band") or "low")
        if band in counts:
            counts[band] += 1

    event = db.create_normalization_event(
        entity_type,
        "refresh_suggestions",
        {
            "limit": limit,
            "use_gemini": bool(use_gemini),
            "generated": len(suggestions),
            "workers": int(workers),
            "bands": counts,
        },
    )

    return {
        "generated": len(suggestions),
        "bands": counts,
        "event": event,
        "workers": int(workers),
    }


def list_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return open suggestions with canonical display names."""
    _entity_config(entity_type)
    try:
        items = db.list_open_suggestions(entity_type, limit=max(1, min(int(limit), 1000)))
        canonicals = db.list_normalization_canonicals(entity_type)
        canonical_names = _canonical_name_map(canonicals)
        payload = []
        for item in items:
            row = dict(item)
            target_id = row.get("target_canonical_id")
            if target_id is not None:
                row["target_canonical_name"] = canonical_names.get(int(target_id), None)
            else:
                row["target_canonical_name"] = None
            payload.append(row)

        return {
            "available": True,
            "error": None,
            "items": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "items": [],
        }



__all__ = ["refresh_suggestions", "list_suggestions"]
