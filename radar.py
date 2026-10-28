#!/usr/bin/env python3
"""Finite X-to-LinkedIn research radar with explicit evidence and cost gates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time as clock
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


URL_RE = re.compile(r"^https?://[^\s]+$")
CONFIDENCE = {"low", "medium", "high"}
SATURATION_STATES = {"unknown", "observed_low", "observed_mixed", "observed_high"}
NEXT_GATES = {"story_interview", "draft", "hold", "reject"}
SCORE_FIELDS = (
    "buyer_relevance",
    "evidence_strength",
    "freshness",
    "differentiation",
    "credible_position",
)
AUTHOR_KINDS = {"human", "organization", "model", "bot", "unknown"}
RELEVANCE = {"direct", "adjacent"}
PROBLEM_EVIDENCE = {"disagreement", "unmet_need", "documented_failure", "none"}
EVIDENCE_PROVENANCE = {"tool_returned", "public_primary_source", "synthetic_fixture"}
VERIFICATION_STATUS = {"verified_public_source", "tool_returned_unverified", "synthetic_fixture"}
HOOK_BASIS = {"public_safe_proof", "pending_owner_question"}
NATIVE_INCLUDE = ["inline_citations", "verbose_streaming", "x_search_call_output"]


def canonical_post_id(url: str) -> str | None:
    """Return X status identity across public URL aliases, independent of handle."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    match = re.fullmatch(r"/(?:i|[A-Za-z0-9_]+)/status/(\d+)/?", parsed.path)
    return match.group(1) if match else None


def normalized_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def url_handle(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    return normalized_handle(parts[0]) if len(parts) == 3 and parts[1] == "status" and parts[0] != "i" else ""


def retained_tool_texts(outputs: Any) -> list[str]:
    if not isinstance(outputs, list):
        return []
    texts = []
    for output in outputs:
        if not isinstance(output, Mapping):
            continue
        message = output.get("message", output)
        if isinstance(message, Mapping) and isinstance(message.get("content"), str):
            texts.append(message["content"])
    return texts

def tool_evidence_matches(text: str, post_id: str, excerpt: str, author_handle: str | None = None) -> bool:
    """Require URL and excerpt in the same structured record/line, never whole-message co-occurrence."""
    if not isinstance(text, str) or not post_id or not excerpt.strip():
        return False
    stripped = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if stripped.startswith(("{", "[")):
            return False
        for line in text.splitlines():
            urls = re.findall(r"https?://(?:www\.)?(?:x|twitter)\.com/[^\s\"'<>|]+", line)
            identities = {canonical_post_id(url) for url in urls}
            if len(identities) != 1 or post_id not in identities or excerpt.strip() not in line:
                continue
            observed_handles = {url_handle(url) for url in urls}
            if "" in observed_handles or len(observed_handles) != 1:
                continue
            if author_handle is None or normalized_handle(author_handle) in observed_handles:
                return True
        return False

    def records(value: Any) -> Iterable[Mapping[str, Any]]:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    yield from records(item)
        elif isinstance(value, Mapping):
            if any(key in value for key in ("post_url", "url", "status_url")):
                yield value
            for key in ("posts", "records", "data", "result"):
                if key in value:
                    yield from records(value[key])

    for record in records(parsed):
        url = next((record[key] for key in ("post_url", "url", "status_url") if isinstance(record.get(key), str)), None)
        body = next((record[key] for key in ("text", "excerpt", "content") if isinstance(record.get(key), str)), None)
        observed_handle = url_handle(url) if url else ""
        if any(key in record and not isinstance(record[key], str) for key in ("author_handle", "author", "handle")):
            continue
        claimed_author = next((record[key] for key in ("author_handle", "author", "handle") if isinstance(record.get(key), str)), None)
        if url and body and canonical_post_id(url) == post_id and excerpt.strip() in body and observed_handle and (claimed_author is None or normalized_handle(claimed_author) == observed_handle) and (author_handle is None or normalized_handle(author_handle) == observed_handle):
            return True
    return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")


def append_receipt(path: str | Path, event: str, **fields: Any) -> dict[str, Any]:
    record = {"timestamp_utc": utc_now(), "event": event, **fields}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {value}")
    return parsed.astimezone(timezone.utc)


def window_bounds(config: Mapping[str, Any]) -> tuple[datetime, datetime]:
    has_start = bool(config.get("window_start_utc")); has_end = bool(config.get("window_end_utc"))
    if has_start != has_end:
        raise ValueError("window_start_utc and window_end_utc must be supplied together")
    if has_start and has_end:
        if config.get("mode") == "diagnostic":
            raise ValueError("diagnostic mode uses its fixed date-only contract")
        start, end = parse_instant(config["window_start_utc"]), parse_instant(config["window_end_utc"])
        if end < start: raise ValueError("window_end_utc must not precede window_start_utc")
        return parse_instant(config["window_start_utc"]), parse_instant(config["window_end_utc"])
    start = datetime.combine(datetime.fromisoformat(config["date_start"]).date(), time.min, timezone.utc)
    end = datetime.combine(datetime.fromisoformat(config["date_end"]).date(), time.max, timezone.utc)
    return start, end


def require_keys(value: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = [key for key in keys if key not in value or value[key] in (None, "")]
    if missing:
        raise ValueError(f"{label} missing required fields: {', '.join(missing)}")


def validate_config(config: Mapping[str, Any]) -> None:
    require_keys(
        config,
        (
            "model",
            "date_start",
            "date_end",
            "max_turns",
            "parallel_tool_calls",
            "reasoning_effort",
            "max_output_tokens",
            "timeout_seconds",
            "application_retries",
            "live_tools",
            "unknown_item_cost_gate",
            "expected_reservation_usd",
            "source_packet",
            "pricing_snapshot",
        ),
        "config",
    )
    if config["model"] != "grok-4.7":
        raise ValueError("model must be grok-4.7 for this approved workflow")
    if config["max_turns"] != 1:
        raise ValueError("max_turns must be 1 for the bounded edition")
    if config["parallel_tool_calls"] is not False:
        raise ValueError("parallel_tool_calls must be false")
    if config["reasoning_effort"] != "low":
        raise ValueError("reasoning_effort must be low")
    if config["application_retries"] != 0:
        raise ValueError("application_retries must be zero")
    if not 1 <= int(config["max_output_tokens"]) <= 8_000:
        raise ValueError("max_output_tokens must be between 1 and 8000")
    if not 1 <= int(config["timeout_seconds"]) <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    if config["live_tools"] != ["x_search"]:
        raise ValueError("the first live edition must use X Search only")
    if config["unknown_item_cost_gate"] is not True:
        raise ValueError("unknown_item_cost_gate must remain enabled")
    start, end = window_bounds(config)
    if end < start:
        raise ValueError("date_end must not precede date_start")
    packet = config["source_packet"]
    require_keys(packet, ("owner", "public_brand_facts", "inventory", "claim_limits"), "source_packet")
    if config.get("mode") == "diagnostic":
        if config["date_start"] != "2026-09-27" or config["date_end"] != "2026-10-03":
            raise ValueError("diagnostic window must be Sep 27-Oct 3 UTC")
        if config["max_output_tokens"] > 1200 or config["expected_reservation_usd"] > 1:
            raise ValueError("diagnostic output and reservation caps exceeded")
        if config.get("topic") != "AI coding-agent completion versus verification":
            raise ValueError("diagnostic topic changed")
        if config.get("diagnostic_scope") != {"post_refs_min": 3, "post_refs_max": 5, "profiles": False, "media": False, "thread_expansion": False}:
            raise ValueError("diagnostic scope changed")
        if config.get("store_messages") is not False or config["pricing_snapshot"].get("hard_fetched_item_cap_available") is not False:
            raise ValueError("diagnostic storage or fetched-item cost condition changed")
    elif config.get("mode", "edition") != "edition":
        raise ValueError("unsupported mode")


def build_prompt(config: Mapping[str, Any], retrieved_at: str) -> str:
    window_text = f"{config.get('window_start_utc', config['date_start'])} through {config.get('window_end_utc', config['date_end'])} UTC"
    if config.get("mode") == "diagnostic":
        return (
            "Source-only diagnostic for one topic: AI coding-agent completion versus verification. "
            f"Use exactly one X Search for public X posts dated {window_text}. Return 3 to 5 public post references. "
            "Do not fetch profiles, expand threads, inspect media, or use any other tool. "
            "Do not develop angles or a content edition. Include only verifiable source metadata. "
            f"Packet prepared at {retrieved_at}; this is not retrieval evidence. "
            "Return one JSON object, no markdown, with this shape: "
            '{"sources":[{"post_url":"https://x.com/handle/status/123",'
            '"published_at":"2026-10-01T12:00:00Z",'
            '"claim_paraphrase":"brief neutral paraphrase"}]}.'
        )
    packet = config["source_packet"]
    schema = {
        "signals": [
            {
                "id": "s1",
                "post_url": "https://x.com/handle/status/123",
                "author_handle": "handle",
                "published_at": "RFC3339 UTC timestamp",
                "retrieved_at": "application replaces this with the actual transport-return UTC timestamp",
                "claim_paraphrase": "faithful paraphrase",
                "counterposition": "credible counterargument",
                "engagement": {
                    "likes": None,
                    "reposts": None,
                    "replies": None,
                    "views": None,
                    "observed_at": "application replaces this with the actual transport-return UTC timestamp",
                    "source": "provider_reported",
                },
                "source_confidence": "low|medium|high",
                "author_kind": "human|organization|model|bot|unknown",
                "story_key": "untrusted topic/story grouping key",
                "relevance": "direct|adjacent",
                "problem_evidence": "disagreement|unmet_need|documented_failure|none",
                "evidence_excerpt": "exact short excerpt or empty when unavailable",
                "evidence_provenance": "tool_returned|public_primary_source|synthetic_fixture",
                "verification_status": "verified_public_source|tool_returned_unverified|synthetic_fixture",
                "candidate_id": "c1",
            }
        ],
        "candidates": [
            {
                "id": "c1",
                "title": "plain-language angle",
                "source_signal_ids": ["s1"],
                "external_argument": "what the sampled conversation argues",
                "counterargument": "best counterposition",
                "buyer": "specific B2B audience",
                "buyer_question": "question this audience is deciding",
                "concrete_problem": "specific B2B problem",
                "useful_consequence": "useful consequence if addressed",
                "owner_position": "credible position grounded in the supplied packet",
                "proof_needed": "owner evidence needed before drafting",
                "existing_inventory_matches": ["published or held collision, if any"],
                "differentiation": "why this is not a generic summary",
                "linkedin_saturation": {"state": "unknown", "evidence": []},
                "scores": {
                    "buyer_relevance": 1,
                    "evidence_strength": 1,
                    "freshness": 1,
                    "differentiation": 1,
                    "credible_position": 1,
                },
                "lag_hypothesis": {"state": "untested", "evidence": [], "uncertainty": "high"},
                "linkedin_format": "text|document|carousel|article|interview-first",
                "format_why": "why that format fits the evidence",
                "hooks": ["2 or 3 owner-review hook hypotheses"],
                "hook_basis": "public_safe_proof|pending_owner_question",
                "hook_basis_detail": "public-safe proof reference or specific owner question",
                "next_gate": "story_interview|draft|hold|reject",
                "disqualifications": ["reasons not to advance"],
                "confidence": "low|medium|high",
            }
        ],
    }
    return (
        "You are producing a bounded research brief, not publishable copy.\n"
        f"Use exactly one X Search tool call. Search public posts published from {window_text}. Do not search or fetch user profiles. Do not use "
        "image or video understanding. Do not widen the time window.\n"
        "Find developing arguments relevant to practical AI, non-traditional builders, enterprise adoption, "
        "agent coordination/verification, real-estate operations, or learning through real projects. Distinguish "
        "a one-off popular post from a developing conversation supported by multiple independent authors. "
        "Engagement is directional only and never proves market growth, buyer demand, or causation.\n"
        "Return one strongest coherent topic and one candidate supported by at least three distinct canonical posts, three attributable human or organization accounts, three distinct story keys, and two direct sources with observable problems. Rebuild it for a LinkedIn owner interview around Owner's credible position; "
        "do not copy X posts or invent first-person scenes, outcomes, metrics, employment status, or buyer demand. "
        "Treat the X-to-LinkedIn lag as an untested hypothesis. LinkedIn saturation must remain unknown because "
        "this request does not retrieve LinkedIn evidence. Use 2 or 3 distinct hooks, each under 280 chars. "
        "Classify author type, relevance, observable problem, story key, evidence provenance and verification uncertainty; "
        "these classifications are untrusted. Quote a short exact tool-returned excerpt for each source when available, otherwise leave it empty. "
        "A pending owner question is a research proposal only. Do not make publish-ready claims.\n"
        f"Packet preparation time: {retrieved_at}. This is not retrieval evidence; the application records the "
        "actual transport-return UTC timestamp after the provider call.\n"
        "Approved public source packet:\n"
        + json.dumps(packet, ensure_ascii=False, indent=2)
        + "\nReturn one JSON object and no markdown. It must match this shape:\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
    )


def _validate_engagement(value: Any, signal_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"signal {signal_id} engagement must be an object")
    require_keys(value, ("observed_at", "source"), f"signal {signal_id} engagement")
    parse_instant(str(value["observed_at"]))
    result = dict(value)
    for key in ("likes", "reposts", "replies", "views"):
        number = result.get(key)
        if number is not None and (not isinstance(number, int) or number < 0):
            raise ValueError(f"signal {signal_id} engagement {key} must be a non-negative integer or null")
    return result


def normalize_signal(raw: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    require_keys(
        raw,
        (
            "id",
            "post_url",
            "author_handle",
            "published_at",
            "retrieved_at",
            "claim_paraphrase",
            "counterposition",
            "engagement",
            "source_confidence",
            "candidate_id",
        ),
        "signal",
    )
    if not URL_RE.match(str(raw["post_url"])):
        raise ValueError(f"signal {raw['id']} has an invalid URL")
    if raw["source_confidence"] not in CONFIDENCE:
        raise ValueError(f"signal {raw['id']} has invalid source_confidence")
    if raw.get("author_kind", "unknown") not in AUTHOR_KINDS or raw.get("relevance", "adjacent") not in RELEVANCE or raw.get("problem_evidence", "none") not in PROBLEM_EVIDENCE:
        raise ValueError(f"signal {raw['id']} has invalid source-selection fields")
    if raw.get("evidence_provenance", "") not in EVIDENCE_PROVENANCE | {""} or raw.get("verification_status", "") not in VERIFICATION_STATUS | {""}:
        raise ValueError(f"signal {raw['id']} has invalid evidence provenance/status")
    if not isinstance(raw.get("story_key", ""), str) or not isinstance(raw.get("evidence_excerpt", ""), str):
        raise ValueError(f"signal {raw['id']} story_key and evidence_excerpt must be strings")
    published = parse_instant(str(raw["published_at"]))
    retrieved = parse_instant(str(raw["retrieved_at"]))
    if retrieved < published:
        raise ValueError(f"signal {raw['id']} was retrieved before publication")
    start, end = window_bounds(config)
    in_window = start <= published <= end
    result = dict(raw)
    for key, default in (("author_kind", "unknown"), ("story_key", ""), ("relevance", "adjacent"), ("problem_evidence", "none"), ("evidence_provenance", ""), ("verification_status", "")):
        result.setdefault(key, default)
    result.setdefault("evidence_excerpt", "")
    result["canonical_post_id"] = canonical_post_id(str(raw["post_url"]))
    result["normalized_author_handle"] = normalized_handle(str(raw["author_handle"]))
    result["engagement"] = _validate_engagement(raw["engagement"], str(raw["id"]))
    result["window_status"] = "in_window" if in_window else "out_of_window"
    result["freshness"] = result["window_status"]
    return result, in_window


def _score_candidate(raw_scores: Any, candidate_id: str) -> tuple[dict[str, float], float]:
    if not isinstance(raw_scores, Mapping):
        raise ValueError(f"candidate {candidate_id} scores must be an object")
    scores: dict[str, float] = {}
    for field in SCORE_FIELDS:
        if field not in raw_scores:
            raise ValueError(f"candidate {candidate_id} score missing {field}")
        score = float(raw_scores[field])
        if not 1 <= score <= 5:
            raise ValueError(f"candidate {candidate_id} score {field} must be 1..5")
        scores[field] = score
    return scores, round(sum(scores.values()) / len(scores), 2)


def normalize_candidate(raw: Mapping[str, Any], signals_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    require_keys(
        raw,
        (
            "id", "title", "source_signal_ids", "external_argument", "counterargument", "proof_needed", "existing_inventory_matches",
            "differentiation", "linkedin_saturation", "scores", "lag_hypothesis", "linkedin_format",
            "format_why", "hooks", "next_gate", "disqualifications", "confidence",
        ),
        "candidate",
    )
    candidate_id = str(raw["id"])
    signal_ids = list(raw["source_signal_ids"])
    if not signal_ids or len(signal_ids) != len(set(signal_ids)):
        raise ValueError(f"candidate {candidate_id} needs unique source_signal_ids")
    missing = [signal_id for signal_id in signal_ids if signal_id not in signals_by_id]
    if missing:
        raise ValueError(f"candidate {candidate_id} cites missing/ineligible signals: {missing}")
    hooks = list(raw["hooks"])
    if not 2 <= len(hooks) <= 3 or any(not str(hook).strip() or len(str(hook)) > 280 for hook in hooks):
        raise ValueError(f"candidate {candidate_id} needs 2-3 non-empty hooks <=280 chars")
    if raw["confidence"] not in CONFIDENCE:
        raise ValueError(f"candidate {candidate_id} has invalid confidence")
    if raw["next_gate"] not in NEXT_GATES:
        raise ValueError(f"candidate {candidate_id} has invalid next_gate")
    saturation = raw["linkedin_saturation"]
    if not isinstance(saturation, Mapping) or saturation.get("state") not in SATURATION_STATES:
        raise ValueError(f"candidate {candidate_id} has invalid LinkedIn saturation state")
    saturation_evidence = saturation.get("evidence", [])
    if not isinstance(saturation_evidence, list) or any(not isinstance(item, str) or not item.strip() for item in saturation_evidence):
        raise ValueError(f"candidate {candidate_id} saturation evidence must be a list of non-empty strings")
    if saturation["state"] != "unknown" or saturation_evidence:
        raise ValueError(f"candidate {candidate_id} LinkedIn saturation must remain unknown without LinkedIn retrieval")
    lag = raw["lag_hypothesis"]
    if not isinstance(lag, Mapping) or lag.get("state") not in {"untested", "supported", "not_supported"}:
        raise ValueError(f"candidate {candidate_id} has invalid lag_hypothesis")
    lag_evidence = lag.get("evidence", [])
    if not isinstance(lag_evidence, list) or any(not isinstance(item, str) or not item.strip() for item in lag_evidence):
        raise ValueError(f"candidate {candidate_id} lag evidence must be a list of non-empty strings")
    if lag["state"] != "untested" or lag_evidence:
        raise ValueError(f"candidate {candidate_id} X-to-LinkedIn lag must remain untested")
    scores, total = _score_candidate(raw["scores"], candidate_id)
    authors = {signals_by_id[signal_id]["normalized_author_handle"] for signal_id in signal_ids}
    result = dict(raw)
    for key in ("concrete_problem", "buyer", "buyer_question", "owner_position", "useful_consequence", "hook_basis", "hook_basis_detail"):
        result.setdefault(key, "")
    result.update({
        "source_signal_ids": signal_ids,
        "source_urls": [signals_by_id[signal_id]["post_url"] for signal_id in signal_ids],
        "independent_author_count": len(authors),
        "conversation_status": "developing_multi_source" if len(authors) >= 2 else "one_off",
        "scores": scores,
        "score_total": total,
        "linkedin_saturation": {"state": saturation["state"], "evidence": saturation_evidence},
        "lag_hypothesis": {"state": lag["state"], "evidence": lag_evidence, "uncertainty": str(lag.get("uncertainty", "high"))},
    })
    return result


def build_edition(config: Mapping[str, Any], response: Mapping[str, Any], *, proof_class: str, provider: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validate_config(config)
    if not isinstance(response, Mapping):
        raise ValueError("provider content must be a JSON object")
    raw_signals = response.get("signals")
    raw_candidates = response.get("candidates")
    if not isinstance(raw_signals, list) or not isinstance(raw_candidates, list):
        raise ValueError("provider content needs signals and candidates arrays")
    signals: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for raw in raw_signals:
        if not isinstance(raw, Mapping):
            raise ValueError("every signal must be an object")
        signal, in_window = normalize_signal(raw, config)
        signal_id = str(signal["id"])
        if signal_id in seen_signal_ids:
            raise ValueError(f"duplicate signal id: {signal_id}")
        seen_signal_ids.add(signal_id)
        (signals if in_window else discarded).append(signal)
    signals_by_id = {str(signal["id"]): signal for signal in signals}
    candidates = [normalize_candidate(raw, signals_by_id) for raw in raw_candidates]
    if len(candidates) > 5:
        raise ValueError("edition must contain 1-5 candidates")
    candidate_ids = [str(candidate["id"]) for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate ids must be unique")
    candidates.sort(key=lambda candidate: (-candidate["score_total"], str(candidate["id"])))
    eligible: list[dict[str, Any]] = []
    rejected_sources: list[dict[str, Any]] = []
    source_reasons: dict[str, list[str]] = {}
    tool_texts = retained_tool_texts((provider or {}).get("tool_outputs", []))
    for signal in signals:
        reasons: list[str] = []
        handle = signal["normalized_author_handle"]
        post_handle = url_handle(str(signal["post_url"]))
        if signal["author_kind"] not in {"human", "organization"} or handle in {"grok", "xai", "bot"} or post_handle in {"grok", "xai", "bot"}:
            reasons.append("ineligible_author")
        if not post_handle or post_handle != handle: reasons.append("unattributable_author")
        if not signal["canonical_post_id"]: reasons.append("invalid_canonical_post")
        if signal["relevance"] != "direct": reasons.append("keyword_adjacency")
        if signal["problem_evidence"] == "none": reasons.append("no_observable_problem_evidence")
        if not signal["story_key"].strip(): reasons.append("missing_story_key")
        if not signal["evidence_excerpt"].strip(): reasons.append("missing_evidence_excerpt")
        pair = (signal["evidence_provenance"], signal["verification_status"])
        if pair not in {("tool_returned", "tool_returned_unverified"), ("public_primary_source", "verified_public_source"), ("synthetic_fixture", "synthetic_fixture")}:
            reasons.append("inconsistent_evidence_provenance")
        if proof_class == "synthetic_fixture" and pair != ("synthetic_fixture", "synthetic_fixture"):
            reasons.append("non_synthetic_fixture_evidence")
        if proof_class != "synthetic_fixture" and pair == ("synthetic_fixture", "synthetic_fixture"):
            reasons.append("synthetic_evidence_in_live_result")
        if proof_class != "synthetic_fixture" and pair == ("public_primary_source", "verified_public_source"):
            reasons.append("public_source_verification_not_retained")
        if pair[0] == "tool_returned" and proof_class != "synthetic_fixture":
            if not any(tool_evidence_matches(text, signal["canonical_post_id"], signal["evidence_excerpt"], signal["author_handle"]) for text in tool_texts):
                reasons.append("tool_evidence_not_retained")
        source_reasons[str(signal["id"])] = reasons
        item = {"signal_id": signal["id"], "reasons": reasons}
        (rejected_sources if reasons else eligible).append(item)
    eligible_by_id = {str(item["signal_id"]): item for item in eligible}
    for candidate in candidates:
        reasons: list[str] = []
        source_signals = [signals_by_id[sid] for sid in candidate["source_signal_ids"]]
        if any(str(s["candidate_id"]) != str(candidate["id"]) for s in source_signals): reasons.append("candidate_topic_mismatch")
        for s in source_signals:
            reasons.extend(source_reasons[str(s["id"])])
        usable = [s for s in source_signals if str(s["id"]) in eligible_by_id]
        unique_posts = {s["canonical_post_id"]: s for s in usable}
        if len(unique_posts) < len(usable): reasons.append("duplicate_canonical_post")
        usable = list(unique_posts.values())
        if len(usable) < 3: reasons.append("fewer_than_3_eligible_posts")
        if len({s["normalized_author_handle"] for s in usable}) < 3: reasons.append("fewer_than_3_unique_accounts")
        if len({str(s["story_key"]).strip().casefold() for s in usable if str(s["story_key"]).strip()}) < 3: reasons.append("duplicate_or_repost_story")
        if len({" ".join(str(s["claim_paraphrase"]).casefold().split()) for s in usable}) < len(usable): reasons.append("possible_syndication_review")
        if sum(s["relevance"] == "direct" for s in usable) < 2: reasons.append("fewer_than_2_direct_sources")
        if sum(s["relevance"] == "direct" and s["problem_evidence"] != "none" for s in usable) < 2: reasons.append("fewer_than_2_direct_problem_sources")
        if not any(s["problem_evidence"] != "none" for s in usable): reasons.append("no_observable_problem_evidence")
        if not all(str(candidate.get(key, "")).strip() for key in ("concrete_problem", "buyer", "buyer_question", "owner_position", "useful_consequence")): reasons.append("missing_mvq_problem_buyer_or_position")
        if not 2 <= len(candidate.get("hooks", [])) <= 3: reasons.append("needs_2_to_3_hooks")
        if len({str(hook).strip().casefold() for hook in candidate["hooks"]}) != len(candidate["hooks"]): reasons.append("duplicate_hooks")
        if candidate.get("hook_basis") not in HOOK_BASIS: reasons.append("missing_or_invalid_hook_basis")
        if not str(candidate.get("hook_basis_detail", "")).strip(): reasons.append("missing_hook_basis_detail")
        if candidate.get("hook_basis") == "pending_owner_question" and candidate["next_gate"] not in {"story_interview", "hold"}:
            reasons.append("pending_owner_question_requires_research_gate")
        candidate["mvq_status"] = "qualified" if not reasons else "insufficient_signal"
        candidate["mvq_rejection_reasons"] = list(dict.fromkeys(reasons))
    status = "qualified" if any(c["mvq_status"] == "qualified" for c in candidates) else "insufficient_signal"
    empty_reason = "no_candidates" if not candidates and not signals else ("no_eligible_sources" if not candidates else None)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    edition = {
        "proof_class": proof_class,
        "generated_at": utc_now(),
        "window": {"from": config.get("window_start_utc", config["date_start"]), "to": config.get("window_end_utc", config["date_end"]), "timezone": "UTC"},
        "signals": signals,
        "discarded_signals": discarded,
        "candidates": candidates,
        "status": status,
        "mvq_status": status,
        "mvq_rejection_reasons": ([empty_reason] if empty_reason else list(dict.fromkeys(reason for candidate in candidates for reason in candidate["mvq_rejection_reasons"]))) if status != "qualified" else [],
        "rejected_sources": rejected_sources,
        "lag_experiment": {
            "status": "proposed_not_run",
            "hypothesis": "A developing X argument may reach a buyer-relevant LinkedIn audience before it saturates there.",
            "design": "Use matched topics and comparable native formats; preregister windows; compare qualified buyer questions, saves, substantive replies, and meetings rather than raw likes.",
            "limits": "One radar edition cannot validate timing, causation, audience overlap, or LinkedIn demand.",
        },
        "provider": dict(provider or {}),
        "limits": [
            "X engagement is directional and does not prove buyer demand or market growth.",
            "Post metadata and content are provider-reported until independently checked at the source URL.",
            "LinkedIn saturation is unknown without retrieved LinkedIn evidence.",
            "Hooks are owner-review hypotheses, not approved copy.",
            "No first-person story may advance without owner-confirmed experience and proof.",
        ],
    }
    edition["edition_digest"] = sha256(edition)
    return edition


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_markdown(edition: Mapping[str, Any]) -> str:
    if edition.get("assessment_kind") == "consumed_diagnostic":
        rows = [f"- `{item['post_url']}`: {', '.join(item['reasons'])}" for item in edition["sources"]]
        return "# X Search diagnostic assessment\n\n**Status: insufficient_signal**\n\nConsumed diagnostic only; no original source facts reconstructed.\n\n" + "\n".join(rows) + "\n\nCandidate reasons: " + ", ".join(edition["mvq_rejection_reasons"]) + "\n"
    date = str(edition["generated_at"])[:10]
    lines = [
        "---", f'title: "X-to-LinkedIn Content Radar — {date}"', f"date_created: {date}", f"date_modified: {date}",
        "type: intelligence-brief", "status: draft", "domains: [marketing, career-brand, communication, coding-agents]",
        "thesis_areas: [credential-erosion, 88-6-gap, new-builder-index]", "related:",
        '  - "[[04_Content/CONTENT_SYSTEM]]"', '  - "[[04_Content/Research/2026-10-02_Grok_X_Content_Review]]"',
        "tags: [xai, x-research, linkedin, owner-review]", "---", "", "# X-to-LinkedIn Content Radar", "",
        f"**Proof class:** `{edition['proof_class']}`  ", f"**Window:** {edition['window']['from']} through {edition['window']['to']} UTC  ",
        f"**Signals retained / discarded:** {len(edition['signals'])} / {len(edition['discarded_signals'])}", "",
        f"**Source-selection MVQ:** `{edition.get('mvq_status', 'unassessed')}`",
        "",
        "> Research brief only. This is not owner acceptance, draft approval, publication authority, a global trend ranking, or evidence that X engagement transfers to LinkedIn.",
        "", "## Ranked candidates", "",
    ]
    for candidate in edition["candidates"]:
        lines.extend([
            f"### {candidate['rank']}. {candidate['title']} — {candidate['score_total']}/5", "",
            f"- **Conversation:** `{candidate['conversation_status']}` across {candidate['independent_author_count']} independent author(s)",
            f"- **Buyer:** {candidate['buyer']}", f"- **Buyer question:** {candidate['buyer_question']}",
            f"- **Sampled argument:** {candidate['external_argument']}", f"- **Counterargument:** {candidate['counterargument']}",
            f"- **Owner's credible position:** {candidate['owner_position']}", f"- **Proof needed:** {candidate['proof_needed']}",
            f"- **Inventory collision:** {', '.join(candidate['existing_inventory_matches']) or 'none recorded'}",
            f"- **Differentiation:** {candidate['differentiation']}", f"- **LinkedIn saturation:** `{candidate['linkedin_saturation']['state']}`",
            f"- **Lag hypothesis:** `{candidate['lag_hypothesis']['state']}`; uncertainty `{candidate['lag_hypothesis']['uncertainty']}`",
            f"- **Format:** {candidate['linkedin_format']} — {candidate['format_why']}", f"- **Next gate:** `{candidate['next_gate']}`",
            f"- **Confidence:** `{candidate['confidence']}`", f"- **Disqualifications:** {', '.join(candidate['disqualifications']) or 'none recorded'}",
            f"- **MVQ decision:** `{candidate.get('mvq_status', 'unassessed')}`; reasons: {', '.join(candidate.get('mvq_rejection_reasons', [])) or 'none'}",
            "", "**Hook hypotheses (owner review):**", "", *[f"- {hook}" for hook in candidate["hooks"]], "",
            "**Evidence:** " + ", ".join(f"[source {index + 1}]({url})" for index, url in enumerate(candidate["source_urls"])), "",
        ])
    lines.extend(["## Signal ledger", "", "| Author | Published | Claim | Counterposition | Engagement | Status |", "| --- | --- | --- | --- | --- | --- |"])
    if not edition["candidates"]:
        lines.extend(["", f"**insufficient_signal:** {', '.join(edition.get('mvq_rejection_reasons', [])) or 'no_candidates'}", ""])
    for signal in edition["signals"]:
        engagement = signal["engagement"]
        engagement_text = "/".join(str(engagement.get(name)) if engagement.get(name) is not None else "?" for name in ("likes", "reposts", "replies", "views"))
        lines.append(f"| [@{signal['normalized_author_handle']}]({signal['post_url']}) | {signal['published_at']} | {signal['claim_paraphrase']} | {signal['counterposition']} | {engagement_text} at {engagement['observed_at']} ({engagement['source']}) | {signal['source_confidence']} / {signal['window_status']} |")
    if edition["discarded_signals"]:
        lines.extend(["", "## Discarded evidence", ""])
        for signal in edition["discarded_signals"]:
            lines.append(f"- `{signal['id']}` — {signal['post_url']} — `{signal['window_status']}`")
    lines.extend(["", "## Measurable lag experiment", "", edition["lag_experiment"]["design"], "", f"**Limit:** {edition['lag_experiment']['limits']}", "", "## Limits and next gate", "", *[f"- {limit}" for limit in edition["limits"]], "", "Candidates marked `story_interview` require the owner's answers before drafting. The owner retains copy and publication acceptance."])
    return "\n".join(lines) + "\n"


def render_html(edition: Mapping[str, Any]) -> str:
    if edition.get("assessment_kind") == "consumed_diagnostic":
        rows = "".join(f"<li>{_escape(item['post_url'])}: {_escape(', '.join(item['reasons']))}</li>" for item in edition["sources"])
        return f"<!doctype html><html lang='en'><meta charset='utf-8'><title>Insufficient signal</title><body><h1>insufficient_signal</h1><p>Consumed diagnostic only. No qualified candidate or publish-ready claim.</p><ul>{rows}</ul><p>Candidate reasons: {_escape(', '.join(edition['mvq_rejection_reasons']))}</p></body></html>"
    nav = "".join(f'<a href="#candidate-{_escape(candidate["id"])}"><span>{candidate["rank"]}</span>{_escape(candidate["title"])}</a>' for candidate in edition["candidates"])
    cards: list[str] = []
    for candidate in edition["candidates"]:
        score_cells = "".join(f"<div><strong>{_escape(label.replace('_', ' '))}</strong><span>{score:g}/5</span></div>" for label, score in candidate["scores"].items())
        hooks = "".join(f"<li>{_escape(hook)}</li>" for hook in candidate["hooks"])
        sources = "".join(f'<a href="{_escape(url)}">Source {index + 1}</a>' for index, url in enumerate(candidate["source_urls"]))
        chips = "".join(f"<span class='chip warn'>{_escape(item)}</span>" for item in candidate["disqualifications"])
        cards.append(f"""
        <article id="candidate-{_escape(candidate['id'])}" class="card"><header><div><p class="eyebrow">Rank {candidate['rank']} · {_escape(candidate['conversation_status'])}</p><h2>{_escape(candidate['title'])}</h2><p><b>MVQ: {_escape(candidate['mvq_status'])}</b> — {_escape(', '.join(candidate['mvq_rejection_reasons']) or 'no rejection reasons')}</p></div><div class="score">{candidate['score_total']:.2f}<small>/5</small></div></header>
        <div class="decision-grid"><div><label>Buyer</label><p>{_escape(candidate['buyer'])}</p></div><div><label>Question</label><p>{_escape(candidate['buyer_question'])}</p></div><div><label>Next gate</label><p>{_escape(candidate['next_gate'])}</p></div><div><label>Confidence</label><p>{_escape(candidate['confidence'])}</p></div></div>
        <section><h3>Argument and counterposition</h3><p>{_escape(candidate['external_argument'])}</p><blockquote>{_escape(candidate['counterargument'])}</blockquote></section>
        <section class="position"><h3>Owner's credible position</h3><p>{_escape(candidate['owner_position'])}</p><p><b>Proof needed:</b> {_escape(candidate['proof_needed'])}</p></section>
        <div class="two"><section><h3>Differentiation</h3><p>{_escape(candidate['differentiation'])}</p><p><b>Inventory:</b> {_escape(', '.join(candidate['existing_inventory_matches']) or 'none recorded')}</p></section><section><h3>Evidence limits</h3><p><b>LinkedIn saturation:</b> {_escape(candidate['linkedin_saturation']['state'])}</p><p><b>Lag:</b> {_escape(candidate['lag_hypothesis']['state'])}; {_escape(candidate['lag_hypothesis']['uncertainty'])} uncertainty</p></section></div>
        <section><h3>Format</h3><p><b>{_escape(candidate['linkedin_format'])}</b> — {_escape(candidate['format_why'])}</p></section><section><h3>Hook hypotheses</h3><ol class="hooks">{hooks}</ol></section><div class="score-grid">{score_cells}</div><div class="foot">{sources}{chips}</div></article>""")
    signal_rows = "".join(f"<tr><td><a href='{_escape(signal['post_url'])}'>@{_escape(signal['normalized_author_handle'])}</a></td><td>{_escape(signal['published_at'])}</td><td>{_escape(signal['claim_paraphrase'])}</td><td>{_escape(signal['counterposition'])}</td><td>{_escape(signal['source_confidence'])}</td></tr>" for signal in edition["signals"])
    limits = "".join(f"<li>{_escape(limit)}</li>" for limit in edition["limits"])
    if not edition["candidates"]: limits += f"<li>insufficient_signal: {_escape(', '.join(edition.get('mvq_rejection_reasons', [])) or 'no_candidates')}</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>X-to-LinkedIn Radar</title><style>
:root{{--ink:#18201f;--muted:#66706d;--paper:#f3f0e8;--card:#fffefb;--line:#d9d3c5;--green:#174f43;--gold:#c7902f;--red:#8f3d2c}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}}a{{color:var(--green)}}.layout{{display:grid;grid-template-columns:270px minmax(0,980px);gap:42px;max-width:1320px;margin:auto;padding:32px}}aside{{position:sticky;top:24px;align-self:start;background:#17342e;color:white;padding:24px;border-radius:18px}}aside h1{{font:700 28px/1.05 Georgia,serif;margin:0 0 12px}}aside p{{color:#d9e5e0}}aside nav{{display:grid;gap:8px;margin-top:24px}}aside nav a{{display:flex;gap:10px;color:white;text-decoration:none;padding:9px;border-radius:9px;background:#ffffff0c}}aside nav span{{color:#e8c780;font-weight:700}}main{{min-width:0}}.hero{{padding:24px 0 10px}}.hero h1{{font:700 46px/1.02 Georgia,serif;margin:0 0 12px}}.meta{{display:flex;gap:10px;flex-wrap:wrap}}.chip{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:5px 9px;font-size:13px;margin:3px;background:white}}.chip.warn{{border-color:#d9ad9f;color:var(--red)}}.notice{{border-left:5px solid var(--gold);background:#fff8e8;padding:16px 18px;margin:22px 0}}.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:25px;margin:20px 0;box-shadow:0 9px 22px #392c1410}}.card header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}h2{{font:700 31px/1.1 Georgia,serif;margin:2px 0 14px}}h3{{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--green);margin:20px 0 6px}}.eyebrow,label{{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:700}}.score{{font:700 34px/1 Georgia,serif;color:var(--green)}}.score small{{font:14px system-ui;color:var(--muted)}}.decision-grid,.two,.score-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.decision-grid div,.score-grid div{{border:1px solid var(--line);border-radius:11px;padding:11px}}.decision-grid p{{margin:4px 0}}blockquote{{margin:10px 0;padding:10px 14px;border-left:3px solid var(--line);color:var(--muted)}}.position{{background:#edf5f1;border-radius:12px;padding:1px 16px 12px}}.hooks{{padding-left:22px}}.hooks li{{padding:6px 0}}.score-grid{{grid-template-columns:repeat(5,minmax(0,1fr));margin-top:18px}}.score-grid div{{display:flex;flex-direction:column}}.score-grid strong{{font-size:11px;text-transform:uppercase;color:var(--muted)}}.score-grid span{{font-weight:700}}.foot{{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:16px}}.foot a{{font-size:14px}}.evidence{{overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:14px}}table{{border-collapse:collapse;width:100%;min-width:780px}}th,td{{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid var(--line)}}th{{background:#e9e5db;font-size:12px;text-transform:uppercase}}.experiment{{background:#1e3d36;color:white;border-radius:18px;padding:24px;margin:24px 0}}.experiment h2{{margin-top:0}}.experiment p{{color:#dbe8e4}}@media(max-width:900px){{.layout{{grid-template-columns:1fr;padding:18px}}aside{{position:static}}aside nav{{display:none}}.hero h1{{font-size:38px}}.score-grid{{grid-template-columns:repeat(2,1fr)}}}}@media(max-width:620px){{.decision-grid,.two{{grid-template-columns:1fr}}.card{{padding:18px}}h2{{font-size:26px}}}}
</style></head><body><div class="layout"><aside><h1>X → LinkedIn Radar</h1><p>Research signals rebuilt for owner review.</p><nav>{nav}</nav></aside><main><section class="hero"><p class="eyebrow">{_escape(edition['window']['from'])} to {_escape(edition['window']['to'])} UTC</p><h1>Conversation radar, not a trend oracle.</h1><div class="meta"><span class="chip">{_escape(edition['proof_class'])}</span><span class="chip"><b>MVQ: {_escape(edition['mvq_status'])}</b></span><span class="chip">{len(edition['signals'])} retained signals</span><span class="chip">{len(edition['discarded_signals'])} discarded</span></div></section><div class="notice"><b>Decision boundary:</b> these are research candidates. They are not the owner's opinion, approved copy, buyer demand, publication authority, or proof that X leads LinkedIn.</div>{''.join(cards)}<section><h2>Signal ledger</h2><div class="evidence"><table><thead><tr><th>Author</th><th>Published</th><th>Claim</th><th>Counterposition</th><th>Confidence</th></tr></thead><tbody>{signal_rows}</tbody></table></div></section><section class="experiment"><h2>Measurable lag experiment</h2><p>{_escape(edition['lag_experiment']['design'])}</p><p><b>Limit:</b> {_escape(edition['lag_experiment']['limits'])}</p></section><section><h2>Limits</h2><ul>{limits}</ul></section></main></div></body></html>"""


def write_review_artifacts(out: Path, edition: Mapping[str, Any], *, event: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "edition.json", edition)
    write_text(out / "review.md", render_markdown(edition))
    write_text(out / "review.html", render_html(edition))
    append_receipt(out / "receipts.jsonl", event, proof_class=edition["proof_class"], edition_digest=edition["edition_digest"], mvq_status=edition.get("mvq_status"), rejection_reasons=edition.get("mvq_rejection_reasons", []), counts={"signals_retained": len(edition.get("signals", [])), "signals_discarded": len(edition.get("discarded_signals", [])), "candidates": len(edition.get("candidates", []))}, files=["edition.json", "review.md", "review.html"])


def assess_diagnostic(diagnostic: Mapping[str, Any], envelope: Mapping[str, Any], out: Path) -> dict[str, Any]:
    if diagnostic.get("mode") != "diagnostic" or not isinstance(diagnostic.get("sources"), list):
        raise ValueError("expected consumed diagnostic result with sources")
    raw_content = json.loads(str(envelope.get("content", "")))
    if not isinstance(raw_content, Mapping) or raw_content.get("sources") != diagnostic["sources"]:
        raise ValueError("diagnostic result does not match the raw provider envelope")
    assessed = []
    for source in diagnostic["sources"]:
        url = str(source.get("post_url", ""))
        parsed = urlparse(url)
        handle = normalized_handle(parsed.path.split("/")[1]) if len(parsed.path.split("/")) > 1 else ""
        reasons = ["missing_relevance", "missing_problem_evidence", "missing_story_key", "missing_evidence_excerpt", "tool_evidence_not_retained"]
        if handle in {"grok", "xai", "bot"}: reasons.insert(0, "ineligible_author")
        if not canonical_post_id(url): reasons.append("invalid_canonical_post")
        assessed.append({"post_url": url, "author_handle": handle, "reasons": reasons})
    result = {"assessment_kind": "consumed_diagnostic", "proof_class": "live_provider_research_unaccepted", "status": "insufficient_signal", "mvq_status": "insufficient_signal", "mvq_rejection_reasons": list(dict.fromkeys(reason for source in assessed for reason in source["reasons"])), "sources": assessed, "provider_response_id": envelope.get("provider_response_id"), "generated_at": utc_now(), "limits": ["Original tool excerpt and relevance are not retained in this consumed diagnostic; no source is qualified."]}
    result["edition_digest"] = sha256(result)
    write_review_artifacts(out, result, event="diagnostic_assessed_locally")
    return result


def _message_to_dict(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(value, preserving_proto_field_name=True)
    except Exception:
        return str(value)


def _find_nested(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        if key in mapping:
            return mapping[key]
        for value in mapping.values():
            found = _find_nested(value, key)
            if found is not None:
                return found
    if isinstance(mapping, list):
        for value in mapping:
            found = _find_nested(value, key)
            if found is not None:
                return found
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    converted = _message_to_dict(value)
    if converted is value:
        return str(value)
    return _json_safe(converted)


def _positive_tool_usage(server_usage: Any) -> dict[str, int]:
    if not isinstance(server_usage, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in server_usage.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            result[str(key).upper()] = count
    return result


def validate_live_envelope(envelope: Mapping[str, Any]) -> None:
    usage = _positive_tool_usage(envelope.get("server_side_tool_usage"))
    x_search_count = usage.get("SERVER_SIDE_TOOL_X_SEARCH", 0)
    unexpected = {name: count for name, count in usage.items() if name != "SERVER_SIDE_TOOL_X_SEARCH"}
    if x_search_count != 1 or unexpected:
        raise ValueError(f"live response must report exactly one X Search and no other server tools; observed={usage}")
    counts = envelope.get("x_item_counts", {})
    if not isinstance(counts, Mapping):
        raise ValueError("live response x_item_counts must be an object")
    profiles = counts.get("profiles_fetched")
    if profiles is not None and (isinstance(profiles, bool) or not isinstance(profiles, int) or profiles != 0):
        raise ValueError(f"live response reported profile retrieval: {profiles}")


def apply_transport_return_time(response: Mapping[str, Any], returned_at: str) -> dict[str, Any]:
    normalized = dict(response)
    raw_signals = normalized.get("signals")
    if not isinstance(raw_signals, list):
        return normalized
    signals: list[Any] = []
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, Mapping):
            signals.append(raw_signal)
            continue
        signal = dict(raw_signal)
        signal["retrieved_at"] = returned_at
        engagement = signal.get("engagement")
        if isinstance(engagement, Mapping):
            engagement = dict(engagement)
            engagement["observed_at"] = returned_at
            signal["engagement"] = engagement
        signals.append(signal)
    normalized["signals"] = signals
    return normalized


def validate_diagnostic_result(value: Any, config: Mapping[str, Any], returned_at: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"sources"}:
        raise ValueError("diagnostic result must contain only sources")
    sources = value["sources"]
    if not isinstance(sources, list) or not 3 <= len(sources) <= 5:
        raise ValueError("diagnostic result requires 3-5 sources")
    start, end = window_bounds(config)
    seen: set[str] = set()
    cleaned = []
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {"post_url", "published_at", "claim_paraphrase"}:
            raise ValueError("diagnostic source has unexpected fields")
        url = source["post_url"]
        if not isinstance(url, str) or not re.fullmatch(r"https://(?:www\.)?x\.com/[A-Za-z0-9_]+/status/[0-9]+/?", url):
            raise ValueError("diagnostic source must use a public X status URL")
        if url in seen:
            raise ValueError("diagnostic source URLs must be unique")
        seen.add(url)
        published = parse_instant(source["published_at"])
        if not start <= published <= end:
            raise ValueError("diagnostic source date is outside window")
        if not isinstance(source["claim_paraphrase"], str) or not source["claim_paraphrase"].strip():
            raise ValueError("diagnostic source needs claim_paraphrase")
        cleaned.append(dict(source))
    return {"mode": "diagnostic", "proof_class": "live_provider_research_unaccepted", "transport_returned_at": returned_at, "sources": cleaned}


def load_prepared_request(config: Mapping[str, Any], prepared_dir: Path) -> tuple[str, dict[str, Any]]:
    if (prepared_dir / "superseded.json").exists():
        raise SystemExit("live unavailable: prepared packet is explicitly superseded")
    prompt_path = prepared_dir / "prompt.txt"
    manifest_path = prepared_dir / "manifest.json"
    authorization_path = prepared_dir / "authorization.json"
    if not prompt_path.is_file() or not manifest_path.is_file() or not authorization_path.is_file():
        raise SystemExit("live unavailable: prepared packet must contain prompt.txt, manifest.json, and authorization.json")
    prompt = prompt_path.read_text(encoding="utf-8")
    manifest = read_json(manifest_path)
    authorization = read_json(authorization_path)
    if manifest.get("status") != "awaiting_human_unknown_item_cost_approval" or manifest.get("provider_calls") != 0:
        raise SystemExit("live unavailable: prepared manifest is not an unused no-send packet")
    if authorization.get("status") != "awaiting_human_unknown_item_cost_approval":
        raise SystemExit("live unavailable: prepared authorization record has an unexpected status")
    if manifest.get("authorization_digest") != sha256(authorization):
        raise SystemExit("live unavailable: prepared authorization record digest mismatch")
    expected = {
        "config_digest": sha256(config),
        "source_packet_digest": sha256(config["source_packet"]),
        "prompt_digest": sha256(prompt),
    }
    mismatches = [name for name, digest in expected.items() if manifest.get(name) != digest]
    if mismatches:
        raise SystemExit(f"live unavailable: prepared packet digest mismatch: {', '.join(mismatches)}")
    expected_request = {
        "model": config["model"], "tools": config["live_tools"], "date_start": config["date_start"],
        "date_end": config["date_end"], "window_start_utc": config.get("window_start_utc"), "window_end_utc": config.get("window_end_utc"), "max_turns": config["max_turns"],
        "parallel_tool_calls": config["parallel_tool_calls"], "reasoning_effort": config["reasoning_effort"],
        "max_output_tokens": config["max_output_tokens"], "timeout_seconds": config["timeout_seconds"],
        "application_retries": config["application_retries"], "include": NATIVE_INCLUDE, "store_messages": False,
    }
    if manifest.get("request") != expected_request:
        raise SystemExit("live unavailable: prepared request fields no longer match config")
    return prompt, manifest


def claim_prepared_request(prepared_dir: Path, approval_id: str, manifest: Mapping[str, Any], out: Path) -> dict[str, Any]:
    claim = {
        "status": "claimed_for_single_transport_attempt",
        "claimed_at": utc_now(),
        "approval_id": approval_id,
        "prepared_manifest_digest": sha256(manifest),
        "live_output": str(out.resolve()),
    }
    claim_path = prepared_dir / "single-use-claim.json"
    try:
        descriptor = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SystemExit("live unavailable: prepared packet was already claimed for a transport attempt") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(claim, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # A partial or unknown claim remains fail-closed; do not remove it and risk a duplicate paid request.
        raise
    return claim


def serialize_native_response(response: Any, config: Mapping[str, Any], sdk_version: str) -> dict[str, Any]:
    usage = _message_to_dict(response.usage)
    x_posts = _find_nested(usage, "x_posts_fetched")
    x_users = _find_nested(usage, "x_users_fetched")
    return {
        "provider": "xai", "sdk": "xai-sdk-python", "sdk_version": sdk_version,
        "provider_response_id": response.id, "model": config["model"],
        "created_at": response.created.isoformat().replace("+00:00", "Z"), "content": response.content,
        "finish_reason": response.finish_reason, "citations": list(response.citations),
        "inline_citations": [_message_to_dict(item) for item in response.inline_citations],
        "tool_calls": [_message_to_dict(call) for call in response.tool_calls],
        "tool_outputs": [_message_to_dict(item) for item in getattr(response, "tool_outputs", [])],
        "server_side_tool_usage": dict(response.server_side_tool_usage), "usage": usage,
        "cost_usd": response.cost_usd,
        "x_item_counts": {"posts_fetched": x_posts, "profiles_fetched": x_users, "availability": "reported" if x_posts is not None or x_users is not None else "not_exposed_by_native_sdk_response"},
        "provider_call_count": 1, "application_retries": 0,
    }


def build_native_chat(config: Mapping[str, Any], prompt: str, client: Any) -> tuple[Any, str]:
    import xai_sdk
    from xai_sdk.chat import user
    from xai_sdk.tools import web_search, x_search

    tools = []
    for name in config["live_tools"]:
        if name == "x_search":
            start, end = window_bounds(config)
            tools.append(x_search(from_date=start, to_date=end, enable_image_understanding=False, enable_video_understanding=False))
        elif name == "web_search":
            tools.append(web_search(enable_image_understanding=False, enable_image_search=False))
        else:
            raise ValueError(f"unsupported live tool: {name}")
    chat = client.chat.create(model=config["model"], tools=tools, max_turns=int(config["max_turns"]), parallel_tool_calls=bool(config["parallel_tool_calls"]), reasoning_effort=config["reasoning_effort"], max_tokens=int(config["max_output_tokens"]), response_format="json_object", include=NATIVE_INCLUDE, store_messages=False)
    chat.append(user(prompt))
    return chat, xai_sdk.__version__


def native_transport(config: Mapping[str, Any], prompt: str, api_key: str, *, observer: Callable[[str, int, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    from xai_sdk import Client
    start = utc_now()
    started_clock = clock.monotonic()
    response = None
    first_response_id = None
    chunk_count = 0
    seen: set[str] = set()
    def emit(event: str, metadata: dict[str, Any] | None = None) -> None:
        if observer is not None and event not in seen:
            observer(event, chunk_count, metadata or {})
        seen.add(event)
    try:
        with Client(api_key=api_key, timeout=float(config["timeout_seconds"]), channel_options=[("grpc.enable_retries", 0)]) as client:
            chat, sdk_version = build_native_chat(config, prompt, client)
            emit("request_dispatched")
            for response, chunk in chat.stream():
                chunk_count += 1
                response_id = getattr(response, "id", None)
                new_response_id = first_response_id is None and isinstance(response_id, str) and bool(response_id)
                if new_response_id:
                    first_response_id = response_id[:200]
                if chunk_count == 1:
                    emit("first_chunk", {"provider_response_id": first_response_id} if first_response_id else None)
                elif new_response_id:
                    emit("response_id_observed", {"provider_response_id": first_response_id})
                if chunk.tool_calls:
                    emit("first_tool_call", {"tool_call_seen": True})
                if chunk.tool_outputs:
                    emit("first_tool_output_if_emitted", {"tool_output_seen": True})
                if chunk.server_side_tool_usage.get("SERVER_SIDE_TOOL_X_SEARCH", 0):
                    emit("first_x_search_usage", {"x_search_usage_seen": True})
                if chunk.citations or chunk.inline_citations:
                    emit("first_citation", {"citation_seen": True})
                if chunk.content:
                    emit("first_content", {"content_seen": True})
            emit("stream_complete")
            if response is None:
                raise RuntimeError("stream completed without a response")
        result=serialize_native_response(response, config, sdk_version); result.update({"application_retries":0,"grpc_retries_enabled":False,"started_at_utc":start,"ended_at_utc":utc_now()}); return result
    except Exception as exc:
        end=utc_now(); code=getattr(exc,"code",lambda:None)(); details=getattr(exc,"details",lambda:None)()
        failure = RuntimeError(_json_safe({"error_type":type(exc).__name__,"grpc_code":str(code)[:100] if code else None,"grpc_details":str(details)[:300] if details else None,"timeout_seconds":config["timeout_seconds"],"started_at_utc":start,"ended_at_utc":end,"provider_acceptance":"unknown","application_retries":0,"grpc_retries_enabled":False}))
        failure.partial_transport_evidence = {
            "status": "partial_unvalidated", "provider_response_id": first_response_id,
            "chunk_count": chunk_count, "observed_transition_flags": {event: event in seen for event in (
                "request_dispatched", "first_chunk", "first_tool_call", "first_tool_output_if_emitted",
                "first_x_search_usage", "first_citation", "first_content", "stream_complete")},
            "started_at_utc": start, "ended_at_utc": end,
            "elapsed_seconds": round(clock.monotonic() - started_clock, 3),
            "provider_acceptance": "response_id_or_chunk_observed" if first_response_id or chunk_count else "unknown_before_first_chunk",
            "application_retries": 0, "grpc_retries_enabled": False,
        }
        raise failure from exc


CANARY_PROMPT = "Return exactly the word CANARY."
STREAM_CANARY_PROMPT = 'Return one JSON object with exactly {"sum":4} for 2+2. No markdown.'


def native_stream_canary(out: Path, *, transport: Callable[..., Mapping[str, Any]] = native_transport,
                         env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One no-tool streaming request through the production native transport."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit("native-stream-canary refuses a non-empty or non-directory output path")
    key = (os.environ if env is None else env).get("XAI_API_KEY")
    if not key:
        raise SystemExit("native-stream-canary unavailable: XAI_API_KEY is missing")
    config = {
        "model": "grok-4.7", "live_tools": [], "max_turns": 1,
        "parallel_tool_calls": False, "reasoning_effort": "low", "max_output_tokens": 128,
        "timeout_seconds": 60, "application_retries": 0,
    }
    manifest = {
        "kind": "native_stream_no_tool_canary", "request": {**config, "response_format": "json_object", "store_messages": False,
        "grpc_retries_enabled": False}, "prompt_digest": sha256(STREAM_CANARY_PROMPT),
        "expected_reservation_usd": 0.25, "provider_calls": 0,
        "max_tokens_note": "128 limits final text, not separately reported reasoning tokens or total billed output; actual provider cost is authoritative.",
    }
    authorization = {
        "status": "authorized_local_canary", "authority_basis": "local development canary authorization",
        "scope": "one grok-4.7 public no-tool streamed request; zero retries; no publication",
        "expected_reservation_usd": 0.25, "provider_calls": 0,
    }
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "manifest.json", manifest)
    write_json(out / "authorization.json", authorization)
    write_text(out / "prompt.txt", STREAM_CANARY_PROMPT)
    started_at = utc_now()
    started_clock = clock.monotonic()
    (out / "transport-progress.jsonl").touch()
    def observe(event: str, chunk_count: int, metadata: dict[str, Any]) -> None:
        append_receipt(out / "transport-progress.jsonl", event,
                       elapsed_seconds=round(clock.monotonic() - started_clock, 3),
                       chunk_count=chunk_count, **metadata)
    append_receipt(out / "receipts.jsonl", "stream_canary_pre_transport", started_at_utc=started_at,
                   expected_reservation_usd=0.25, provider_calls=0)
    try:
        envelope = _json_safe(dict(transport(config, STREAM_CANARY_PROMPT, key, observer=observe)))
    except Exception as exc:
        ended_at = utc_now()
        partial = getattr(exc, "partial_transport_evidence", None)
        if partial is not None:
            write_json(out / "partial-transport-evidence.json", partial)
        result = {
            "transport_status": "failed", "content_contract_status": "not_evaluated",
            "provider_response_id": partial.get("provider_response_id") if partial else None,
            "usage": None, "cost_usd": None, "started_at_utc": started_at, "ended_at_utc": ended_at,
            "elapsed_seconds": round(clock.monotonic() - started_clock, 3),
            "provider_acceptance": partial.get("provider_acceptance") if partial else "unknown_before_first_chunk",
            "application_retries": 0, "grpc_retries_enabled": False,
        }
        write_json(out / "result.json", result)
        append_receipt(out / "receipts.jsonl", "stream_canary_transport_failed", **result)
        raise
    ended_at = utc_now()
    envelope["transport_returned_at"] = ended_at
    write_json(out / "raw-provider-envelope.json", envelope)
    write_text(out / "provider-content.txt", str(envelope.get("content", "")))
    try:
        parsed = json.loads(str(envelope.get("content", "")))
        content_valid = isinstance(parsed, dict) and set(parsed) == {"sum"} and type(parsed["sum"]) is int and parsed["sum"] == 4
    except (TypeError, ValueError):
        content_valid = False
    result = {
        "transport_status": "returned", "content_contract_status": "passed" if content_valid else "failed",
        "provider_response_id": envelope.get("provider_response_id"), "usage": envelope.get("usage"),
        "cost_usd": envelope.get("cost_usd"), "started_at_utc": started_at, "ended_at_utc": ended_at,
        "elapsed_seconds": round(clock.monotonic() - started_clock, 3),
        "application_retries": 0, "grpc_retries_enabled": False,
    }
    write_json(out / "result.json", result)
    append_receipt(out / "receipts.jsonl", "stream_canary_returned", **result)
    return result


def native_canary(out: Path, *, client_factory: Callable[..., Any] | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One no-tool SDK request; deliberately independent of the X-search packet."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit("native-canary refuses a non-empty or non-directory output path")
    key = (os.environ if env is None else env).get("XAI_API_KEY")
    if not key: raise SystemExit("native-canary unavailable: XAI_API_KEY is missing")
    out.mkdir(parents=True, exist_ok=True)
    start = utc_now(); authority="local development canary authorization"; scope="same xAI/grok-4.7/public no-tool scope; one call; zero retries; no publication"
    manifest = {"kind":"native_no_tool_canary","model":"grok-4.7","prompt":CANARY_PROMPT,"reasoning_effort":"low","max_tokens":64,"timeout_seconds":60,"tool_count":0,"calls":1,"application_retries":0,"reservation_usd":0.10,"authority_basis":authority,"scope":scope,"started_at_utc":start}
    write_json(out/"manifest.json",manifest); write_json(out/"authorization.json",{"status":"authorized_local_canary","authority_basis":authority,"scope":scope,"reservation_usd":0.10,"provider_calls":0}); append_receipt(out/"receipts.jsonl","canary_pre_transport",**manifest)
    if client_factory is None:
        import xai_sdk
        from xai_sdk import Client
        client_factory = Client
        sdk_version = xai_sdk.__version__
    else:
        sdk_version = "injected-test-client"
    end=None
    try:
        with client_factory(api_key=key, timeout=60, channel_options=[("grpc.enable_retries", 0)]) as client:
            from xai_sdk.chat import user
            chat=client.chat.create(model="grok-4.7", tools=[], max_tokens=64, reasoning_effort="low", max_turns=1, parallel_tool_calls=False, store_messages=False)
            chat.append(user(CANARY_PROMPT)); response=chat.sample()
        end=utc_now(); result={"status":"returned","sdk_version":getattr(client,"__version__",None),"provider_response_id":getattr(response,"id",None),"usage":getattr(response,"usage",None),"cost_usd":getattr(response,"cost_usd",None),"started_at_utc":start,"ended_at_utc":end,"elapsed_seconds":round((parse_instant(end)-parse_instant(start)).total_seconds(),3),"application_retries":0}
        result["sdk_version"]=sdk_version; result["content"]=_json_safe(getattr(response,"content", "")); result["content_digest"]=sha256(str(result["content"]))
        result["usage"]=_json_safe(result["usage"]); result["cost_usd"]=_json_safe(result["cost_usd"])
        if str(result["content"]).strip() != "CANARY":
            result["status"]="contract_failed"; write_json(out/"result.json",result); append_receipt(out/"receipts.jsonl","canary_contract_failed",**result); raise SystemExit("native-canary contract failed: response content was not CANARY")
    except Exception as exc:
        end=utc_now(); code=getattr(exc,"code",lambda:None)(); details=getattr(exc,"details",lambda:None)(); result={"status":"failed","error_type":type(exc).__name__,"grpc_code":str(code)[:100] if code else None,"grpc_details":str(details)[:300] if details else None,"started_at_utc":start,"ended_at_utc":end,"elapsed_seconds":round((parse_instant(end)-parse_instant(start)).total_seconds(),3),"application_retries":0}
        write_json(out/"result.json",result); append_receipt(out/"receipts.jsonl","canary_failed",**result); raise
    write_json(out/"result.json",result); append_receipt(out/"receipts.jsonl","canary_returned",**result); return result


def prepare_live(config: Mapping[str, Any], out: Path) -> dict[str, Any]:
    validate_config(config)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit("prepare-live refuses to overwrite a non-empty or non-directory packet path")
    prepared_at = utc_now()
    prompt = build_prompt(config, prepared_at)
    authorization = {"status": "awaiting_human_unknown_item_cost_approval", "question": "Approve one X Search request despite no provider-enforced fetched-item maximum?", "expected_reservation_usd": config["expected_reservation_usd"], "hard_maximum_usd": None, "scope": "one X Search request; public data; no profiles requested; no retry; no publication"}
    if config.get("mode") == "diagnostic":
        authorization["max_tokens_note"] = "1200 is the text/final-output max_tokens setting, not a hard cap on total billed output tokens; provider-reported reasoning and actual cost remain authoritative."
    manifest = {
        "status": "awaiting_human_unknown_item_cost_approval", "prepared_at": prepared_at, "provider": "xai",
        "sdk": "xai-sdk-python==1.20.0",
        "request": {"model": config["model"], "tools": config["live_tools"], "date_start": config["date_start"], "date_end": config["date_end"], "window_start_utc": config.get("window_start_utc"), "window_end_utc": config.get("window_end_utc"), "max_turns": config["max_turns"], "parallel_tool_calls": config["parallel_tool_calls"], "reasoning_effort": config["reasoning_effort"], "max_output_tokens": config["max_output_tokens"], "timeout_seconds": config["timeout_seconds"], "application_retries": config["application_retries"], "include": NATIVE_INCLUDE, "store_messages": False},
        "prompt_digest": sha256(prompt), "config_digest": sha256(config), "source_packet_digest": sha256(config["source_packet"]), "authorization_digest": sha256(authorization),
        "pricing_snapshot": config["pricing_snapshot"], "expected_reservation_usd": config["expected_reservation_usd"],
        "hard_item_cost_cap_available": False, "provider_calls": 0,
    }
    out.mkdir(parents=True, exist_ok=True)
    write_text(out / "prompt.txt", prompt)
    write_json(out / "manifest.json", manifest)
    write_json(out / "authorization.json", authorization)
    append_receipt(out / "receipts.jsonl", "prepared_no_send", **manifest)
    return manifest


def live_run(config: Mapping[str, Any], out: Path, *, approval_id: str, prepared_dir: Path, transport: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]] = native_transport, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    validate_config(config)
    api_key = (os.environ if env is None else env).get("XAI_API_KEY")
    if not api_key:
        raise SystemExit("live unavailable: XAI_API_KEY is missing from the child process environment")
    if not approval_id.strip():
        raise SystemExit("live unavailable: an explicit unknown-item-cost approval id is required")
    prompt, prepared_manifest = load_prepared_request(config, prepared_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit("live refuses to use a non-empty or non-directory output path; choose a fresh run directory")
    started_at = utc_now()
    out.mkdir(parents=True, exist_ok=True)
    write_text(out / "prompt.txt", prompt)
    claim = claim_prepared_request(prepared_dir, approval_id, prepared_manifest, out)
    preflight = {"status": "authorized_for_one_request", "approval_id": approval_id, "started_at": started_at, "config_digest": sha256(config), "source_packet_digest": sha256(config["source_packet"]), "prompt_digest": sha256(prompt), "prepared_manifest_digest": sha256(prepared_manifest), "single_use_claim_digest": sha256(claim), "expected_reservation_usd": config["expected_reservation_usd"], "hard_item_cost_cap_available": False, "application_retries": 0}
    write_json(out / "authorization-used.json", preflight)
    append_receipt(out / "receipts.jsonl", "authorized_pre_transport", **preflight)
    progress_started = clock.monotonic()
    def observe(event: str, chunk_count: int, metadata: dict[str, Any]) -> None:
        append_receipt(out / "transport-progress.jsonl", event,
                       elapsed_seconds=round(clock.monotonic() - progress_started, 3),
                       chunk_count=chunk_count, **metadata)
    try:
        if transport is native_transport:
            (out / "transport-progress.jsonl").touch()
            envelope = _json_safe(dict(transport(config, prompt, api_key, observer=observe)))
        else:
            envelope = _json_safe(dict(transport(config, prompt, api_key)))
    except Exception as exc:
        ended_at=utc_now(); detail=str(exc)[:500]
        acceptance = "unknown_if_transport_did_not_return"
        if transport is native_transport and hasattr(exc, "partial_transport_evidence"):
            write_json(out / "partial-transport-evidence.json", exc.partial_transport_evidence)
            acceptance = exc.partial_transport_evidence["provider_acceptance"]
        append_receipt(out / "receipts.jsonl", "transport_failed_no_retry", error_type=type(exc).__name__, error=detail, configured_timeout_seconds=config["timeout_seconds"], started_at_utc=started_at, ended_at_utc=ended_at, elapsed_seconds=round((parse_instant(ended_at)-parse_instant(started_at)).total_seconds(),3), provider_acceptance=acceptance, application_retries=0, grpc_retries_enabled=False)
        raise
    transport_returned_at = utc_now()
    envelope["transport_returned_at"] = transport_returned_at
    write_text(out / "provider-content.txt", str(envelope.get("content", "")))
    append_receipt(out / "receipts.jsonl", "provider_returned_unvalidated", provider_response_id=envelope.get("provider_response_id"), model=envelope.get("model"), transport_returned_at=transport_returned_at, cost_usd=envelope.get("cost_usd"), usage=envelope.get("usage"), tool_calls=envelope.get("tool_calls"), server_side_tool_usage=envelope.get("server_side_tool_usage"), x_item_counts=envelope.get("x_item_counts"), citations=envelope.get("citations"), raw_envelope_digest=sha256(envelope), application_retries=0)
    write_json(out / "raw-provider-envelope.json", envelope)
    try:
        validate_live_envelope(envelope)
        parsed = json.loads(str(envelope.get("content", "")))
        if config.get("mode") == "diagnostic":
            edition = validate_diagnostic_result(parsed, config, transport_returned_at)
        else:
            parsed = apply_transport_return_time(parsed, transport_returned_at)
            edition = build_edition(config, parsed, proof_class="live_provider_research_unaccepted", provider={key: value for key, value in envelope.items() if key != "content"})
    except Exception as exc:
        append_receipt(out / "receipts.jsonl", "validation_failed_no_retry", provider_response_id=envelope.get("provider_response_id"), cost_usd=envelope.get("cost_usd"), error_type=type(exc).__name__, error=str(exc), application_retries=0)
        raise
    if config.get("mode") == "diagnostic":
        write_json(out / "diagnostic-result.json", edition)
        append_receipt(out / "receipts.jsonl", "live_diagnostic_validated", source_count=len(edition["sources"]), provider_calls=1)
    else:
        write_review_artifacts(out, edition, event="live_edition_validated")
    return edition


def fixture_run(config: Mapping[str, Any], response: Mapping[str, Any], out: Path) -> dict[str, Any]:
    edition = build_edition(config, response, proof_class="synthetic_fixture")
    write_review_artifacts(out, edition, event="synthetic_fixture_validated")
    return edition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fixture = sub.add_parser("fixture", help="Run the deterministic pipeline against a local fixture")
    fixture.add_argument("--config", required=True); fixture.add_argument("--provider-response", required=True); fixture.add_argument("--out", required=True)
    prepare = sub.add_parser("prepare-live", help="Write the exact no-send live request packet")
    prepare.add_argument("--config", required=True); prepare.add_argument("--out", required=True)
    live = sub.add_parser("live", help="Run one explicitly approved xAI request")
    live.add_argument("--config", required=True); live.add_argument("--prepared-dir", required=True); live.add_argument("--out", required=True); live.add_argument("--allow-unknown-item-cost", action="store_true"); live.add_argument("--approval-id")
    render = sub.add_parser("render", help="Re-render a validated edition without provider use")
    render.add_argument("--edition-json", required=True); render.add_argument("--out", required=True)
    assess = sub.add_parser("assess-diagnostic", help="Assess an unchanged consumed diagnostic locally")
    assess.add_argument("--diagnostic-result", required=True); assess.add_argument("--raw-envelope", required=True); assess.add_argument("--out", required=True)
    canary = sub.add_parser("native-canary", help="One no-tool, low-cost SDK connectivity canary")
    canary.add_argument("--out", required=True)
    stream_canary = sub.add_parser("native-stream-canary", help="One no-tool streamed SDK contract canary")
    stream_canary.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fixture":
        fixture_run(read_json(args.config), read_json(args.provider_response), Path(args.out)); return 0
    if args.command == "assess-diagnostic":
        assess_diagnostic(read_json(args.diagnostic_result), read_json(args.raw_envelope), Path(args.out)); return 0
    if args.command == "prepare-live":
        prepare_live(read_json(args.config), Path(args.out)); return 0
    if args.command == "live":
        if not args.allow_unknown_item_cost:
            raise SystemExit("live is gated: explicit approval of unknown fetched-item cost is required")
        if not args.approval_id:
            raise SystemExit("live is gated: --approval-id is required")
        live_run(read_json(args.config), Path(args.out), approval_id=args.approval_id, prepared_dir=Path(args.prepared_dir)); return 0
    if args.command == "native-canary":
        native_canary(Path(args.out)); return 0
    if args.command == "native-stream-canary":
        result = native_stream_canary(Path(args.out))
        return 0 if result["content_contract_status"] == "passed" else 1
    edition = read_json(args.edition_json)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    write_text(out / "review.md", render_markdown(edition)); write_text(out / "review.html", render_html(edition))
    append_receipt(out / "receipts.jsonl", "local_render", proof_class=edition.get("proof_class"), edition_digest=edition.get("edition_digest"), provider_calls=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
