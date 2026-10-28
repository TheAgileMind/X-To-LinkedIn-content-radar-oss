import copy
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
import radar  # noqa: E402


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.config = radar.read_json(ROOT / "fixture" / "config.json")
        self.response = radar.read_json(ROOT / "fixture" / "response.json")
        self.output_root = ROOT / "test-output"
        self.output_root.mkdir(exist_ok=True)

    def output_dir(self, label):
        path = self.output_root / f"{label}-{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def prepared_dir(self, label="prepared"):
        path = self.output_dir(label)
        radar.prepare_live(self.config, path)
        return path

    def test_mvq_qualified_coherent_three_account_candidate(self):
        response = self.coherent_response()
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("qualified", edition["mvq_status"])

    def test_missing_evidence_is_retained_as_insufficient_signal(self):
        response = copy.deepcopy(self.response)
        response["signals"][0]["evidence_excerpt"] = ""
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("insufficient_signal", edition["mvq_status"])
        self.assertIn("missing_evidence_excerpt", edition["candidates"][0]["mvq_rejection_reasons"])

    def coherent_response(self):
        response = copy.deepcopy(self.response)
        third = response["signals"][2]
        third["candidate_id"] = "c1"
        third["claim_paraphrase"] = "A third team reports agent verification gaps in its workflow."
        third["story_key"] = "third-verification-case"
        response["candidates"][0]["source_signal_ids"] = ["s1", "s2", "s3"]
        response["candidates"][0].update(concrete_problem="Agent completion is accepted without outcome checks", useful_consequence="Fewer unverified releases", hook_basis="pending_owner_question", hook_basis_detail="Which public example can the owner discuss?")
        return response

    def test_canonical_aliases_and_repost_story_do_not_inflate_mvq(self):
        response = self.coherent_response()
        duplicate = copy.deepcopy(response["signals"][0]); duplicate.update(id="s8", post_url="https://twitter.com/OTHER/status/1000000000000000001", author_handle="@OTHER", story_key="invented-new-story")
        response["signals"].append(duplicate)
        response["candidates"][0]["source_signal_ids"] = ["s1", "s2", "s8"]
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("insufficient_signal", edition["candidates"][0]["mvq_status"])
        self.assertIn("duplicate_canonical_post", edition["candidates"][0]["mvq_rejection_reasons"])
        response["candidates"][0]["source_signal_ids"] = ["s1", "s2", "s3"]
        response["signals"][2]["story_key"] = response["signals"][1]["story_key"]
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertIn("duplicate_or_repost_story", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_excluded_bot_adjacency_and_missing_evidence_surface_reasons(self):
        response = self.coherent_response()
        response["signals"][0].update(author_handle="@Grok", author_kind="model")
        response["signals"][1]["relevance"] = "adjacent"
        response["signals"][2]["evidence_excerpt"] = ""
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        reasons = edition["candidates"][0]["mvq_rejection_reasons"]
        for reason in ("ineligible_author", "keyword_adjacency", "missing_evidence_excerpt"):
            self.assertIn(reason, reasons)
        self.assertEqual("insufficient_signal", edition["mvq_status"])

    def test_qualified_requires_structural_basis_and_three_coherent_posts(self):
        response = self.coherent_response()
        self.assertEqual("qualified", radar.build_edition(self.config, response, proof_class="synthetic_fixture")["mvq_status"])
        response["candidates"][0]["hook_basis_detail"] = ""
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertIn("missing_hook_basis_detail", edition["candidates"][0]["mvq_rejection_reasons"])
        response["candidates"][0]["hook_basis_detail"] = "Owner question"
        response["candidates"][0]["hooks"] = ["Same hook", "Same hook"]
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertIn("duplicate_hooks", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_two_direct_sources_must_each_show_problem_evidence(self):
        response = self.coherent_response()
        response["signals"][1]["problem_evidence"] = "none"
        response["signals"][2]["problem_evidence"] = "none"
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("insufficient_signal", edition["candidates"][0]["mvq_status"])
        self.assertIn("fewer_than_2_direct_problem_sources", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_pending_owner_question_cannot_advance_to_draft(self):
        response = self.coherent_response()
        response["candidates"][0]["next_gate"] = "draft"
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("insufficient_signal", edition["candidates"][0]["mvq_status"])
        self.assertIn("pending_owner_question_requires_research_gate", edition["candidates"][0]["mvq_rejection_reasons"])
        response["candidates"][0].update(hook_basis="public_safe_proof", hook_basis_detail="Public-safe source artifact")
        edition = radar.build_edition(self.config, response, proof_class="synthetic_fixture")
        self.assertEqual("qualified", edition["candidates"][0]["mvq_status"])

    def test_live_tool_evidence_must_match_serialized_output(self):
        response = self.coherent_response()
        for signal in response["signals"][:3]:
            signal.update(evidence_provenance="tool_returned", verification_status="tool_returned_unverified")
        provider = {"tool_outputs": [{"content": "https://x.com/example_a/status/1000000000000000001 reported completion before verification"}]}
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])
        provider["tool_outputs"][0]["content"] = "\n".join(f"{s['post_url']} {s['evidence_excerpt']}" for s in response["signals"][:3])
        self.assertEqual("qualified", radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)["mvq_status"])
        provider["tool_outputs"] = [{"message": {"content": response["signals"][0]["post_url"]}}, {"message": {"content": response["signals"][0]["evidence_excerpt"]}}]
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_structured_tool_records_bind_each_excerpt_to_its_post(self):
        response = self.coherent_response()
        signals = response["signals"][:3]
        for signal in signals:
            signal.update(evidence_provenance="tool_returned", verification_status="tool_returned_unverified")
        posts = [{"post_url": signal["post_url"], "text": signal["evidence_excerpt"]} for signal in signals]
        provider = {"tool_outputs": [{"message": {"content": json.dumps({"posts": posts})}}]}
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertEqual("qualified", edition["mvq_status"])
        response["signals"][0]["evidence_excerpt"] = signals[1]["evidence_excerpt"]
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertEqual("insufficient_signal", edition["mvq_status"])
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_ambiguous_multi_post_line_cannot_bind_swapped_excerpt(self):
        response = self.coherent_response()
        original = copy.deepcopy(response["signals"][:3])
        for signal in response["signals"][:3]:
            signal.update(evidence_provenance="tool_returned", verification_status="tool_returned_unverified")
        response["signals"][0]["evidence_excerpt"] = original[1]["evidence_excerpt"]
        content = " | ".join(s["post_url"] + " " + s["evidence_excerpt"] for s in original)
        provider = {"tool_outputs": [{"message": {"content": content}}]}
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertEqual("insufficient_signal", edition["mvq_status"])
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_retained_record_author_must_match_selected_author(self):
        response = self.coherent_response()
        original = copy.deepcopy(response["signals"][:3])
        for signal in response["signals"][:3]:
            signal.update(evidence_provenance="tool_returned", verification_status="tool_returned_unverified")
        posts = [{"post_url": s["post_url"], "text": s["evidence_excerpt"], "author_handle": s["author_handle"]} for s in original]
        provider = {"tool_outputs": [{"message": {"content": json.dumps({"posts": posts})}}]}
        self.assertEqual("qualified", radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)["mvq_status"])
        response["signals"][0]["post_url"] = "https://twitter.com/EXAMPLE_A/status/1000000000000000001"
        self.assertEqual("qualified", radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)["mvq_status"])
        response["signals"][0]["author_handle"] = "invented_author"
        response["signals"][0]["post_url"] = "https://x.com/invented_author/status/1000000000000000001"
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertEqual("insufficient_signal", edition["mvq_status"])
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])
        response["signals"][0]["author_handle"] = original[0]["author_handle"]
        response["signals"][0]["post_url"] = original[0]["post_url"]
        posts[0]["post_url"] = "https://x.com/i/status/1000000000000000001"
        posts[0].pop("author_handle")
        provider["tool_outputs"][0]["message"]["content"] = json.dumps({"posts": posts})
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])
        posts[0]["post_url"] = original[0]["post_url"]
        posts[0]["author_handle"] = "conflicting_author"
        provider["tool_outputs"][0]["message"]["content"] = json.dumps({"posts": posts})
        edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
        self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_malformed_or_unattributable_tool_records_reject_in_edition(self):
        response = self.coherent_response()
        signal = response["signals"][0]
        signal.update(evidence_provenance="tool_returned", verification_status="tool_returned_unverified")
        for content in (json.dumps({"data": {"posts": [{"post_url": signal["post_url"]}, {"text": signal["evidence_excerpt"]}]}}),
                        json.dumps({"unrecognized": {"post_url": signal["post_url"], "text": signal["evidence_excerpt"]}}),
                        '{"post_url": "' + signal["post_url"] + '", "text": "' + signal["evidence_excerpt"]):
            provider = {"tool_outputs": [{"message": {"content": content}}]}
            edition = radar.build_edition(self.config, response, proof_class="live_provider_research_unaccepted", provider=provider)
            self.assertEqual("insufficient_signal", edition["mvq_status"])
            self.assertIn("tool_evidence_not_retained", edition["candidates"][0]["mvq_rejection_reasons"])

    def test_native_serialization_retains_tool_message_content(self):
        class Response:
            id = "r"; content = "{}"; finish_reason = "REASON_STOP"; citations = []; inline_citations = []; tool_calls = []
            tool_outputs = [{"message": {"content": "https://x.com/a/status/123 excerpt"}}]
            server_side_tool_usage = {"SERVER_SIDE_TOOL_X_SEARCH": 1}; usage = {}; cost_usd = 0.01
            created = radar.parse_instant("2026-10-03T12:00:00Z")
        envelope = radar.serialize_native_response(Response(), self.config, "1.20.0")
        self.assertEqual(Response.tool_outputs, envelope["tool_outputs"])
        self.assertNotIn("reasoning", json.dumps(envelope))

    def test_consumed_diagnostic_assessment_is_insufficient(self):
        # Synthetic consumed-diagnostic packet: keeps this unit test provider-free
        # while retaining the historical assessment's ineligible-account case.
        sources = [{"post_url": "https://x.com/grok/status/1234567890123456789"}]
        diagnostic = {"mode": "diagnostic", "sources": sources}
        envelope = {"content": json.dumps({"sources": sources}), "provider_response_id": "synthetic-diagnostic"}
        out = self.output_dir("assess-diagnostic")
        result = radar.assess_diagnostic(diagnostic, envelope, out)
        self.assertEqual("insufficient_signal", result["mvq_status"])
        self.assertIn("ineligible_author", json.dumps(result))
        self.assertIn("missing_evidence_excerpt", json.dumps(result))
        self.assertIn("insufficient_signal", (out / "review.html").read_text(encoding="utf-8"))

    def test_fixture_builds_ranked_review_and_discards_out_of_window(self):
        out = self.output_dir("fixture")
        edition = radar.fixture_run(self.config, self.response, out)
        self.assertEqual(4, len(edition["candidates"]))
        self.assertEqual(6, len(edition["signals"]))
        self.assertEqual(1, len(edition["discarded_signals"]))
        self.assertEqual("developing_multi_source", edition["candidates"][0]["conversation_status"])
        self.assertGreaterEqual(edition["candidates"][0]["score_total"], edition["candidates"][-1]["score_total"])
        self.assertTrue((out / "edition.json").is_file())
        self.assertIn("Conversation radar, not a trend oracle", (out / "review.html").read_text(encoding="utf-8"))
        self.assertNotIn("@@", (out / "review.md").read_text(encoding="utf-8"))
        self.assertNotIn("@@", (out / "review.html").read_text(encoding="utf-8"))
        self.assertIn("type: intelligence-brief", (out / "review.md").read_text(encoding="utf-8"))

    def test_native_canary_fake_client_records_bounded_request(self):
        class Response:
            id="canary-1"; content="CANARY"; usage={"total_tokens": 8}; cost_usd=0.001
        class Chat:
            def append(self, message): self.message=message
            def sample(self): return Response()
        class Client:
            def __init__(self, **kwargs): self.kwargs=kwargs; Client.kwargs=kwargs
            def __enter__(self): return self
            def __exit__(self, *args): self.closed=True; Client.closed=True
            class chat:
                @staticmethod
                def create(**kwargs): Client.created=kwargs; return Chat()
        out=self.output_dir("native-canary")
        result=radar.native_canary(out, client_factory=Client, env={"XAI_API_KEY":"placeholder"})
        self.assertEqual("returned", result["status"]); self.assertEqual(0, Client.created["tools"].__len__())
        self.assertEqual([("grpc.enable_retries", 0)], Client.kwargs["channel_options"]); self.assertFalse(Client.created["store_messages"]); self.assertTrue(getattr(Client, "closed", False))

    def test_native_canary_refuses_nonempty_output(self):
        out=self.output_dir("native-nonempty"); (out/"x").write_text("x")
        with self.assertRaises(SystemExit): radar.native_canary(out, env={"XAI_API_KEY":"placeholder"})

    def test_native_canary_rejects_wrong_content_without_retry(self):
        class R: id="bad"; content="not canary"; usage=object(); cost_usd=0.01
        class C:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self,*args): pass
            class chat:
                @staticmethod
                def create(**kwargs):
                    class H:
                        def append(self, *_): pass
                        def sample(self): return R()
                    return H()
        out=self.output_dir("native-wrong");
        with self.assertRaises(SystemExit): radar.native_canary(out, client_factory=C, env={"XAI_API_KEY":"placeholder"})
        self.assertEqual("contract_failed", radar.read_json(out/"result.json")["status"])
        self.assertIn("canary_contract_failed", (out/"receipts.jsonl").read_text())

    def test_native_stream_canary_uses_injected_transport_and_preserves_artifacts(self):
        calls = []
        def fake_transport(config, prompt, api_key, *, observer):
            calls.append((config, prompt, api_key))
            observer("request_dispatched", 0, {})
            observer("first_chunk", 1, {"provider_response_id": "stream-canary-test"})
            observer("first_content", 1, {"content_seen": True})
            observer("stream_complete", 1, {})
            return {"provider_response_id": "stream-canary-test", "content": '{"sum":4}',
                    "usage": {"completion_tokens": 2, "reasoning_tokens": 9}, "cost_usd": 0.001}
        out = self.output_dir("stream-canary")
        result = radar.native_stream_canary(out, transport=fake_transport, env={"XAI_API_KEY": "placeholder"})
        self.assertEqual(1, len(calls))
        self.assertEqual([], calls[0][0]["live_tools"])
        self.assertEqual(128, calls[0][0]["max_output_tokens"])
        self.assertEqual("returned", result["transport_status"])
        self.assertEqual("passed", result["content_contract_status"])
        self.assertEqual(0.001, result["cost_usd"])
        self.assertEqual(0, radar.read_json(out / "manifest.json")["provider_calls"])
        self.assertEqual("authorized_local_canary", radar.read_json(out / "authorization.json")["status"])
        self.assertEqual("stream-canary-test", radar.read_json(out / "raw-provider-envelope.json")["provider_response_id"])
        self.assertEqual('{"sum":4}', (out / "provider-content.txt").read_text(encoding="utf-8"))
        progress = (out / "transport-progress.jsonl").read_text(encoding="utf-8")
        self.assertIn("first_content", progress)
        self.assertNotIn('{"sum":4}', progress)
        with self.assertRaisesRegex(SystemExit, "non-empty"):
            radar.native_stream_canary(out, transport=fake_transport, env={"XAI_API_KEY": "placeholder"})

    def test_native_stream_canary_failure_writes_partial_evidence(self):
        def fake_failure(config, prompt, api_key, *, observer):
            observer("request_dispatched", 0, {})
            observer("first_chunk", 1, {"provider_response_id": "partial-canary"})
            error = RuntimeError("synthetic stream interruption")
            error.partial_transport_evidence = {"status": "partial_unvalidated", "provider_response_id": "partial-canary",
                                                "chunk_count": 1, "provider_acceptance": "response_id_or_chunk_observed",
                                                "application_retries": 0, "grpc_retries_enabled": False}
            raise error
        out = self.output_dir("stream-canary-failure")
        with self.assertRaises(RuntimeError):
            radar.native_stream_canary(out, transport=fake_failure, env={"XAI_API_KEY": "placeholder"})
        result = radar.read_json(out / "result.json")
        self.assertEqual("failed", result["transport_status"])
        self.assertEqual("not_evaluated", result["content_contract_status"])
        self.assertEqual("partial-canary", radar.read_json(out / "partial-transport-evidence.json")["provider_response_id"])
        self.assertFalse((out / "raw-provider-envelope.json").exists())

    def test_prepare_live_is_no_send_and_writes_exact_packet(self):
        out = self.output_dir("prepare")
        manifest = radar.prepare_live(self.config, out)
        self.assertEqual(0, manifest["provider_calls"])
        self.assertFalse(manifest["hard_item_cost_cap_available"])
        self.assertEqual(["x_search"], manifest["request"]["tools"])
        self.assertEqual(radar.NATIVE_INCLUDE, manifest["request"]["include"])
        self.assertTrue((out / "prompt.txt").is_file())
        self.assertIn("exactly one X Search", (out / "prompt.txt").read_text(encoding="utf-8"))
        self.assertEqual("awaiting_human_unknown_item_cost_approval", radar.read_json(out / "authorization.json")["status"])

    def test_cli_live_requires_unknown_cost_gate(self):
        with self.assertRaisesRegex(SystemExit, "explicit approval"):
            radar.main(["live", "--config", str(ROOT / "fixture" / "config.json"), "--prepared-dir", "unused", "--out", "unused"])

    def test_live_requires_key_and_approval_id(self):
        out = self.output_dir("gates")
        prepared = self.prepared_dir("gates-prepared")
        with self.assertRaisesRegex(SystemExit, "XAI_API_KEY"):
            radar.live_run(self.config, out, approval_id="approval", prepared_dir=prepared, env={})
        with self.assertRaisesRegex(SystemExit, "approval id"):
            radar.live_run(self.config, out, approval_id="", prepared_dir=prepared, env={"XAI_API_KEY": "placeholder"})

    def test_live_fake_preserves_raw_before_validation_and_records_usage(self):
        calls = []

        def fake_transport(config, prompt, api_key):
            calls.append((config["model"], len(prompt), api_key))
            return {
                "provider": "xai",
                "sdk_version": "test",
                "provider_response_id": "resp_test",
                "model": "grok-4.7",
                "content": json.dumps(self.response),
                "citations": ["https://x.com/example_a/status/1000000000000000001"],
                "tool_calls": [{"function": {"name": "x_keyword_search", "arguments": "{}"}}],
                "server_side_tool_usage": {"SERVER_SIDE_TOOL_X_SEARCH": 1},
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                "cost_usd": 0.123,
                "x_item_counts": {"posts_fetched": None, "profiles_fetched": None, "availability": "not_exposed_by_native_sdk_response"},
                "provider_call_count": 1,
                "application_retries": 0,
            }

        out = self.output_dir("live-valid")
        prepared = self.prepared_dir("live-valid-prepared")
        edition = radar.live_run(self.config, out, approval_id="human-test", prepared_dir=prepared, transport=fake_transport, env={"XAI_API_KEY": "placeholder"})
        self.assertEqual(1, len(calls))
        self.assertEqual("live_provider_research_unaccepted", edition["proof_class"])
        raw = radar.read_json(out / "raw-provider-envelope.json")
        self.assertEqual("resp_test", raw["provider_response_id"])
        self.assertEqual(raw["transport_returned_at"], edition["signals"][0]["retrieved_at"])
        self.assertEqual(raw["transport_returned_at"], edition["signals"][0]["engagement"]["observed_at"])
        self.assertNotEqual(self.response["signals"][0]["retrieved_at"], edition["signals"][0]["retrieved_at"])
        receipts = [json.loads(line) for line in (out / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["authorized_pre_transport", "provider_returned_unvalidated", "live_edition_validated"], [item["event"] for item in receipts])
        self.assertEqual(0.123, receipts[1]["cost_usd"])

    def test_invalid_paid_output_is_preserved_and_not_retried(self):
        calls = []

        def invalid_transport(config, prompt, api_key):
            calls.append(1)
            return {"provider_response_id": "resp_bad", "model": "grok-4.7", "content": "not json", "cost_usd": 0.07, "usage": {}, "tool_calls": [], "server_side_tool_usage": {"SERVER_SIDE_TOOL_X_SEARCH": 1}, "citations": [], "x_item_counts": {"profiles_fetched": None}}

        out = self.output_dir("live-invalid")
        prepared = self.prepared_dir("live-invalid-prepared")
        with self.assertRaises(json.JSONDecodeError):
            radar.live_run(self.config, out, approval_id="human-test", prepared_dir=prepared, transport=invalid_transport, env={"XAI_API_KEY": "placeholder"})
        self.assertEqual([1], calls)
        self.assertEqual("not json", (out / "provider-content.txt").read_text(encoding="utf-8"))
        self.assertTrue((out / "raw-provider-envelope.json").is_file())
        receipts = [json.loads(line) for line in (out / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual("validation_failed_no_retry", receipts[-1]["event"])

    def test_transport_failure_has_zero_application_retry(self):
        calls = []

        def failed_transport(config, prompt, api_key):
            calls.append(1)
            raise TimeoutError("synthetic timeout")

        out = self.output_dir("live-failed")
        prepared = self.prepared_dir("live-failed-prepared")
        with self.assertRaises(TimeoutError):
            radar.live_run(self.config, out, approval_id="human-test", prepared_dir=prepared, transport=failed_transport, env={"XAI_API_KEY": "placeholder"})
        self.assertEqual([1], calls)
        receipt = json.loads((out / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("transport_failed_no_retry", receipt["event"])
        self.assertEqual(0, receipt["application_retries"])

    def test_schema_rejects_bad_hooks_and_unsupported_saturation(self):
        bad = copy.deepcopy(self.response)
        bad["candidates"][0]["hooks"] = ["only one"]
        with self.assertRaisesRegex(ValueError, "needs 2-3"):
            radar.build_edition(self.config, bad, proof_class="synthetic_fixture")
        bad = copy.deepcopy(self.response)
        bad["candidates"][0]["linkedin_saturation"] = {"state": "observed_high", "evidence": []}
        with self.assertRaisesRegex(ValueError, "must remain unknown"):
            radar.build_edition(self.config, bad, proof_class="synthetic_fixture")

    def test_live_binds_prepared_packet_and_refuses_reuse(self):
        prepared = self.prepared_dir("binding-prepared")
        superseded = self.prepared_dir("superseded-prepared")
        (superseded / "superseded.json").write_text('{"status":"superseded"}\n', encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "explicitly superseded"):
            radar.live_run(self.config, self.output_dir("superseded-live"), approval_id="human-test", prepared_dir=superseded, transport=lambda *_: {}, env={"XAI_API_KEY": "placeholder"})
        changed = copy.deepcopy(self.config)
        changed["date_start"] = "2026-09-28"
        with self.assertRaisesRegex(SystemExit, "digest mismatch"):
            radar.live_run(changed, self.output_dir("binding-live"), approval_id="human-test", prepared_dir=prepared, transport=lambda *_: {}, env={"XAI_API_KEY": "placeholder"})
        occupied = self.output_dir("occupied-live")
        (occupied / "existing.txt").write_text("do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "non-empty or non-directory output path"):
            radar.live_run(self.config, occupied, approval_id="human-test", prepared_dir=prepared, transport=lambda *_: {}, env={"XAI_API_KEY": "placeholder"})
        valid_envelope = {
            "provider_response_id": "resp_once", "model": "grok-4.7", "content": json.dumps(self.response),
            "cost_usd": 0.01, "usage": {}, "tool_calls": [],
            "server_side_tool_usage": {"SERVER_SIDE_TOOL_X_SEARCH": 1}, "citations": [],
            "x_item_counts": {"posts_fetched": 4, "profiles_fetched": None},
        }
        radar.live_run(self.config, self.output_dir("claimed-live"), approval_id="human-test", prepared_dir=prepared, transport=lambda *_: valid_envelope, env={"XAI_API_KEY": "placeholder"})
        with self.assertRaisesRegex(SystemExit, "already claimed"):
            radar.live_run(self.config, self.output_dir("claimed-again"), approval_id="human-test", prepared_dir=prepared, transport=lambda *_: valid_envelope, env={"XAI_API_KEY": "placeholder"})
        self.assertTrue((prepared / "single-use-claim.json").is_file())

    def test_live_rejects_extra_tools_profiles_and_non_unknown_linkedin_claims(self):
        prepared = self.prepared_dir("policy-prepared")

        def envelope(**updates):
            value = {
                "provider_response_id": "resp_policy", "model": "grok-4.7", "content": json.dumps(self.response),
                "cost_usd": 0.01, "usage": {}, "tool_calls": [],
                "server_side_tool_usage": {"SERVER_SIDE_TOOL_X_SEARCH": 1}, "citations": [],
                "x_item_counts": {"posts_fetched": 4, "profiles_fetched": None},
            }
            value.update(updates)
            return value

        extra_out = self.output_dir("extra-tools")
        with self.assertRaisesRegex(ValueError, "exactly one X Search"):
            radar.live_run(self.config, extra_out, approval_id="human-test", prepared_dir=prepared, transport=lambda *_: envelope(server_side_tool_usage={"SERVER_SIDE_TOOL_X_SEARCH": 2}), env={"XAI_API_KEY": "placeholder"})
        self.assertTrue((extra_out / "raw-provider-envelope.json").is_file())
        self.assertIn("validation_failed_no_retry", (extra_out / "receipts.jsonl").read_text(encoding="utf-8"))
        prepared = self.prepared_dir("profiles-prepared")
        with self.assertRaisesRegex(ValueError, "profile retrieval"):
            radar.live_run(self.config, self.output_dir("profiles"), approval_id="human-test", prepared_dir=prepared, transport=lambda *_: envelope(x_item_counts={"posts_fetched": 4, "profiles_fetched": 1}), env={"XAI_API_KEY": "placeholder"})
        prepared = self.prepared_dir("saturation-prepared")
        saturated = copy.deepcopy(self.response)
        saturated["candidates"][0]["linkedin_saturation"] = {"state": "observed_high", "evidence": ["model assertion"]}
        with self.assertRaisesRegex(ValueError, "must remain unknown"):
            radar.live_run(self.config, self.output_dir("saturation"), approval_id="human-test", prepared_dir=prepared, transport=lambda *_: envelope(content=json.dumps(saturated)), env={"XAI_API_KEY": "placeholder"})

    @unittest.skipUnless(os.environ.get("RADAR_TEST_NATIVE_SDK") == "1", "set in isolated xAI SDK venv")
    def test_native_sdk_contract_can_build_request_without_transport(self):
        import xai_sdk
        from xai_sdk import Client
        from xai_sdk.chat import Chunk
        from xai_sdk.proto import chat_pb2

        self.assertEqual("1.20.0", xai_sdk.__version__)
        prompt = radar.build_prompt(self.config, "2026-10-03T00:00:00Z")
        with Client(api_key="placeholder", timeout=300, channel_options=[("grpc.enable_retries", 0)]) as client:
            chat, sdk_version = radar.build_native_chat(self.config, prompt, client)
            self.assertEqual("1.20.0", sdk_version)
            self.assertEqual("grok-4.7", chat.proto.model)
            self.assertEqual(4000, chat.proto.max_tokens)
            self.assertEqual(1, chat.proto.max_turns)
            self.assertFalse(chat.proto.parallel_tool_calls)
            self.assertEqual(1, len(chat.proto.tools))
            self.assertTrue(chat.proto.tools[0].HasField("x_search"))
            self.assertEqual(1, len(chat.proto.messages))
            self.assertIn(chat_pb2.INCLUDE_OPTION_X_SEARCH_CALL_OUTPUT, chat.proto.include)
            self.assertEqual(3, len(chat.proto.include))
        chunk = Chunk(chat_pb2.GetChatCompletionChunk(), 0)
        for property_name in ("tool_calls", "tool_outputs", "server_side_tool_usage", "citations", "inline_citations", "content"):
            getattr(chunk, property_name)

    def test_diagnostic_prompt_packet_and_validation(self):
        config = radar.read_json(ROOT / "fixture" / "diagnostic-config.json")
        prompt = radar.build_prompt(config, "2026-10-03T00:00:00Z")
        for phrase in ("LinkedIn", "hooks", "buyer", "candidates", "developing arguments"):
            self.assertNotIn(phrase, prompt)
        self.assertIn("AI coding-agent completion versus verification", prompt)
        prepared = self.output_dir("diagnostic-prepared")
        manifest = radar.prepare_live(config, prepared)
        self.assertEqual(radar.sha256(prompt.replace("2026-10-03T00:00:00Z", manifest["prepared_at"])), manifest["prompt_digest"])
        self.assertEqual(radar.sha256(config), manifest["config_digest"])
        self.assertEqual(0, manifest["provider_calls"])
        source = lambda n: {"post_url": f"https://x.com/person/status/{n}", "published_at": "2026-10-01T12:00:00Z", "claim_paraphrase": "A neutral summary"}
        valid = {"sources": [source(1), source(2), source(3)]}
        self.assertEqual(3, len(radar.validate_diagnostic_result(valid, config, "2026-10-03T01:00:00Z")["sources"]))
        for bad in ({"sources": [source(1)]}, {"sources": [source(1), source(2), {**source(3), "post_url": "https://example.com/x"}]}, {"sources": [source(1), source(2), {**source(3), "published_at": "2026-09-26T12:00:00Z"}]}, {**valid, "candidates": []}):
            with self.assertRaises(ValueError):
                radar.validate_diagnostic_result(bad, config, "2026-10-03T01:00:00Z")

    def test_native_stream_transition_ledger_and_default_live_wiring(self):
        config = radar.read_json(ROOT / "fixture" / "diagnostic-config.json")
        prepared = self.output_dir("stream-prepared")
        radar.prepare_live(config, prepared)
        out = self.output_dir("stream-live")
        source = lambda n: {"post_url": f"https://x.com/person/status/{n}", "published_at": "2026-10-01T12:00:00Z", "claim_paraphrase": "A neutral summary"}
        class Chunk:
            tool_calls = [object()]; tool_outputs = [object()]
            server_side_tool_usage = {"SERVER_SIDE_TOOL_X_SEARCH": 1}
            citations = ["citation"]; inline_citations = []
            content = "private content"
        class Response:
            id = "response-partial"
        class Chat:
            def stream(self):
                yield Response(), Chunk()
        class Client:
            def __init__(self, **kwargs): self.kwargs = kwargs
            def __enter__(self): return self
            def __exit__(self, *args): self.closed = True
        envelope = {"content": json.dumps({"sources": [source(1), source(2), source(3)]}), "server_side_tool_usage": {"SERVER_SIDE_TOOL_X_SEARCH": 1}, "x_item_counts": {"profiles_fetched": 0}, "provider_response_id": "response-partial"}
        with patch("xai_sdk.Client", Client), patch.object(radar, "build_native_chat", return_value=(Chat(), "test")), patch.object(radar, "serialize_native_response", return_value=envelope):
            result = radar.live_run(config, out, approval_id="human-test", prepared_dir=prepared, env={"XAI_API_KEY": "placeholder"})
        self.assertEqual(3, len(result["sources"]))
        self.assertTrue((out / "diagnostic-result.json").is_file())
        self.assertFalse((out / "edition.json").exists())
        progress = (out / "transport-progress.jsonl").read_text(encoding="utf-8")
        events = [json.loads(line)["event"] for line in progress.splitlines()]
        self.assertEqual(["request_dispatched", "first_chunk", "first_tool_call", "first_tool_output_if_emitted", "first_x_search_usage", "first_citation", "first_content", "stream_complete"], events)
        self.assertNotIn("private content", progress)

    def test_native_stream_timeout_keeps_dispatch_and_partial_id(self):
        class Response:
            id = "partial-id"
        class Chunk:
            tool_calls = []; tool_outputs = []
            server_side_tool_usage = {}; citations = []; inline_citations = []
            content = ""
        class Chat:
            def stream(self):
                yield Response(), Chunk()
                raise TimeoutError("stream interrupted")
        class Client:
            closed = False
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): Client.closed = True
        prepared = self.prepared_dir("partial-prepared")
        out = self.output_dir("partial-live")
        with patch("xai_sdk.Client", Client), patch.object(radar, "build_native_chat", return_value=(Chat(), "test")):
            with self.assertRaises(RuntimeError):
                radar.live_run(self.config, out, approval_id="human-test", prepared_dir=prepared, env={"XAI_API_KEY": "placeholder"})
        progress = [json.loads(line) for line in (out / "transport-progress.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["request_dispatched", "first_chunk"], [item["event"] for item in progress])
        self.assertEqual("partial-id", progress[1]["provider_response_id"])
        evidence = radar.read_json(out / "partial-transport-evidence.json")
        self.assertEqual("partial_unvalidated", evidence["status"])
        self.assertEqual("partial-id", evidence["provider_response_id"])
        self.assertEqual(1, evidence["chunk_count"])
        self.assertEqual("response_id_or_chunk_observed", evidence["provider_acceptance"])
        self.assertTrue(evidence["observed_transition_flags"]["first_chunk"])
        self.assertFalse(evidence["observed_transition_flags"]["stream_complete"])
        self.assertEqual(0, evidence["application_retries"])
        self.assertTrue(Client.closed)
        class NoChunkChat:
            def stream(self):
                raise TimeoutError("before first chunk")
                yield
        prepared = self.prepared_dir("pre-chunk-prepared")
        out = self.output_dir("pre-chunk-live")
        with patch("xai_sdk.Client", Client), patch.object(radar, "build_native_chat", return_value=(NoChunkChat(), "test")):
            with self.assertRaises(RuntimeError):
                radar.live_run(self.config, out, approval_id="human-test", prepared_dir=prepared, env={"XAI_API_KEY": "placeholder"})
        progress = [json.loads(line) for line in (out / "transport-progress.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["request_dispatched"], [item["event"] for item in progress])
        evidence = radar.read_json(out / "partial-transport-evidence.json")
        self.assertEqual("partial_unvalidated", evidence["status"])
        self.assertIsNone(evidence["provider_response_id"])
        self.assertEqual(0, evidence["chunk_count"])
        self.assertEqual("unknown_before_first_chunk", evidence["provider_acceptance"])


    @unittest.skipUnless(os.environ.get("RADAR_TEST_NATIVE_SDK") == "1", "set in isolated xAI SDK venv")
    def test_exact_window_reaches_x_search_request(self):
        config=copy.deepcopy(self.config); config["window_start_utc"]="2026-09-27T07:00:00Z"; config["window_end_utc"]="2026-10-04T06:59:59.999999Z"
        radar.validate_config(config); prompt=radar.build_prompt(config,"2026-10-03T00:00:00Z")
        self.assertIn(config["window_start_utc"],prompt); self.assertIn(config["window_end_utc"],prompt)
        from xai_sdk import Client
        with Client(api_key="placeholder", timeout=300, channel_options=[("grpc.enable_retries",0)]) as client:
            chat,_=radar.build_native_chat(config,prompt,client); tool=chat.proto.tools[0].x_search
            self.assertEqual(1790492400, tool.from_date.seconds); self.assertEqual(1791097199, tool.to_date.seconds); self.assertEqual(999999000, tool.to_date.nanos)

    def test_exact_window_requires_both_and_order(self):
        one=copy.deepcopy(self.config); one["window_start_utc"]="2026-09-27T07:00:00Z"
        with self.assertRaises(ValueError): radar.validate_config(one)
        reversed_config=copy.deepcopy(self.config); reversed_config["window_start_utc"]="2026-10-04T07:00:00Z"; reversed_config["window_end_utc"]="2026-09-27T07:00:00Z"
        with self.assertRaises(ValueError): radar.validate_config(reversed_config)

    def test_diagnostic_rejects_exact_window_override(self):
        diagnostic=copy.deepcopy(self.config); diagnostic["mode"]="diagnostic"; diagnostic["window_start_utc"]="2026-09-27T07:00:00Z"; diagnostic["window_end_utc"]="2026-10-04T06:59:59.999999Z"
        with self.assertRaisesRegex(ValueError,"diagnostic mode"):
            radar.validate_config(diagnostic)

    def test_tool_evidence_requires_same_record(self):
        text=json.dumps({"posts":[{"url":"https://x.com/a/status/1","excerpt":"e1"},{"url":"https://x.com/b/status/2","excerpt":"e2"}]})
        self.assertTrue(radar.tool_evidence_matches(text,"1","e1"))
        self.assertFalse(radar.tool_evidence_matches(text,"1","e2"))
        self.assertFalse(radar.tool_evidence_matches("malformed output","1","e1"))
        single=json.dumps({"post_url":"https://x.com/a/status/1","text":"e1"})
        self.assertTrue(radar.tool_evidence_matches(single,"1","e1"))
        self.assertFalse(radar.tool_evidence_matches(single,"1","e2"))

    def test_empty_fixture_is_insufficient_signal_with_artifacts(self):
        out=self.output_dir("empty-fixture"); edition=radar.fixture_run(self.config,{"signals":[],"candidates":[]},out)
        self.assertEqual("insufficient_signal",edition["status"]); self.assertEqual(["no_candidates"],edition["mvq_rejection_reasons"])
        for name in ("edition.json","review.md","review.html","receipts.jsonl"): self.assertTrue((out/name).is_file())

    def test_empty_fixture_cli_writes_visible_rejection_and_receipt(self):
        source = self.output_dir("empty-cli-source") / "response.json"
        radar.write_json(source, {"signals": [], "candidates": []})
        out = self.output_dir("empty-cli-output")
        self.assertEqual(0, radar.main(["fixture", "--config", str(ROOT / "fixture" / "config.json"), "--provider-response", str(source), "--out", str(out)]))
        edition = radar.read_json(out / "edition.json")
        self.assertEqual("insufficient_signal", edition["status"])
        self.assertEqual([], edition["candidates"])
        self.assertEqual(["no_candidates"], edition["mvq_rejection_reasons"])
        for name in ("review.md", "review.html"):
            self.assertIn("no_candidates", (out / name).read_text(encoding="utf-8"))
        receipt = json.loads((out / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("insufficient_signal", receipt["mvq_status"])
        self.assertEqual(["no_candidates"], receipt["rejection_reasons"])
        self.assertEqual(0, receipt["counts"]["candidates"])

if __name__ == "__main__":
    unittest.main()
