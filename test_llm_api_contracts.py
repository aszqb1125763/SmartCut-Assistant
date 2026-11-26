# -*- coding: utf-8 -*-
"""Remote role contracts, cache separation and failure propagation."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import ai_api_contracts as contracts
import llm_gateway
import script_writer


class RemoteContractTests(unittest.TestCase):
    def client(self, role="editor", **overrides):
        settings = {"enabled": True, "provider": "openai", "protocol": "openai",
                    "base_url": "https://example.test/v1", "model": "remote-model",
                    "api_key": "private-key"}
        settings.update(overrides)
        return llm_gateway.OpenAICompatibleClient(settings, role=role)

    def test_openai_endpoints_preserve_provider_api_versions(self):
        cases = {
            "https://host.test": "https://host.test/v1/chat/completions",
            "https://host.test/v1/": "https://host.test/v1/chat/completions",
            "https://open.bigmodel.cn/api/paas/v4/":
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "https://dashscope.aliyuncs.com/compatible-mode/v1":
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "https://host.test/custom/chat/completions?route=x":
                "https://host.test/custom/chat/completions?route=x",
        }
        for base, expected in cases.items():
            with self.subTest(base=base):
                self.assertEqual(expected, llm_gateway._openai_endpoint(base))

    def test_malformed_api_address_still_raises_remote_failure_type(self):
        for url in ("https://[", "https://host.test:invalid", "https://user@"):
            with self.subTest(url=url):
                with self.assertRaises(llm_gateway.APIRequestError):
                    self.client(base_url=url).chat("contract", "data")

    def test_writer_api_receives_independent_output_contract(self):
        client = self.client("writer")
        user = script_writer.PROMPT_READ % "U0001 | U | t=1-2 | 文本=搭建机器"
        with mock.patch.object(client, "_send", return_value=
                               "摘要：搭建机器\n重要度：4\n关键：U0001") as sent:
            answer = client.chat(script_writer.SYS_READ + " LOCAL_ONLY_MARKER", user)
        remote_system = sent.call_args.args[1]
        self.assertNotIn("LOCAL_ONLY_MARKER", remote_system)
        self.assertIn("严格输出三行", remote_system)
        self.assertIn("有效内容", remote_system)
        self.assertIn("禁止为凑时长保留空场", remote_system)
        self.assertEqual(["搭建机器", "4", "U0001"],
                         [line.split("：", 1)[-1] for line in answer.splitlines()])

    def test_writer_repairs_invented_source_ids(self):
        client = self.client("writer")
        user = script_writer.PROMPT_READ % "U0001 | U | t=1-2 | 文本=搭建机器"
        with mock.patch.object(client, "_send", side_effect=[
                "摘要：搭建机器\n重要度：4\n关键：U9999",
                "摘要：搭建机器\n重要度：4\n关键：U0001"]) as sent:
            answer = client.chat(script_writer.SYS_READ, user)
        self.assertEqual(2, sent.call_count)
        self.assertTrue(answer.endswith("U0001"))

    def test_writer_plan_keeps_parser_required_b_ids(self):
        client = self.client("writer")
        user = script_writer.PROMPT_PLAN.format(
            level_rule="目标保留52%的有效内容", body=
            "B001 / t=0~10秒 / d=10秒 / 开始 / 重要度 4\n"
            "B002 / t=10~20秒 / d=10秒 / 完成 / 重要度 5")
        with mock.patch.object(client, "_send", side_effect=[
                "1|0|20|建造完成|全部保留\n主线：建造机器",
                "1|B001|B002|建造完成|全部保留\n主线：建造机器"]) as sent:
            answer = client.chat(script_writer.SYS_PLAN, user)
        self.assertEqual(2, sent.call_count)
        self.assertIn("|B001|B002|", answer)

    def test_event_json_repairs_wrong_schema_and_rejects_read_only_id(self):
        client = self.client()
        user = ("【前一批只读上下文｜不可删除：U0001】\nU0001 t=0-1\n"
                "【当前批可写事件｜只允许这些 ID：U0002,G0001】\nU0002 t=1-2\nG0001 t=2-4")
        system = '输出JSON，delete_events和delete_ranges使用事件ID。'
        for wrong in ('{"delete":[[0,1]],"reason":"旧格式"}',
                      '{"delete_events":["U0001"],"delete_ranges":[],"reason":"越权"}',
                      '{"delete_events":[],"delete_ranges":[[1,2]],"reason":"自由秒数"}'):
            with self.subTest(wrong=wrong), mock.patch.object(client, "_send", side_effect=[
                    wrong, '{"delete_events":["G0001"],"delete_ranges":[],"reason":"等待"}']) as sent:
                result = json.loads(client.chat(system, user))
            self.assertEqual(2, sent.call_count)
            self.assertEqual(["G0001"], result["delete_events"])

    def test_valid_json_missing_fields_fails_after_one_repair(self):
        client = self.client()
        with mock.patch.object(client, "_send", return_value='{"ok":true}') as sent:
            with self.assertRaises(llm_gateway.APIRequestError):
                client.chat("输出JSON delete_ranges", "0~10秒的序列")
        self.assertEqual(2, sent.call_count)

    def test_pr_step_contract_retains_done_without_event_ids(self):
        import pr_engine
        client = self.client()
        with mock.patch.object(client, "_send", return_value=
                               '{"done":true,"reason":"当前序列完整"}') as sent:
            result = json.loads(client.chat(pr_engine.STEP_SYS, "序列总长10秒"))
        self.assertTrue(result["done"])
        remote_system = sent.call_args.args[1]
        self.assertIn('"done":true', remote_system)
        self.assertNotIn("delete_events", remote_system)

    def test_pr_range_contract_repairs_times_outside_current_batch(self):
        client = self.client()
        user = "0001 10.00~12.00 目标\n0002 14.00~16.00 结果"
        with mock.patch.object(client, "_send", side_effect=[
                '{"delete_ranges":[[0,20]],"reason":"越界"}',
                '{"delete_ranges":[[12,14]],"reason":"等待"}']) as sent:
            answer = client.chat("输出JSON delete_ranges", user)
        self.assertEqual(2, sent.call_count)
        self.assertEqual([[12, 14]], json.loads(answer)["delete_ranges"])

    def test_director_review_and_continuity_use_separate_contracts(self):
        import director
        import longchain
        client = self.client("director")
        review_user = director.REVIEW.format(
            picked="1", goal="做机器", level_hint="保留目标和结果",
            lines="1. 目标\n2. 完成")
        with mock.patch.object(client, "_send", side_effect=[
                "NOT OK", "ADD 2"]) as sent:
            self.assertEqual("ADD 2", client.chat(director.SYS_SEG, review_user))
        self.assertEqual(2, sent.call_count)
        with mock.patch.object(client, "_send", return_value="结果：通\n理由：因果完整\n缺口：无") as sent:
            client.chat(longchain.SYSTEM, longchain.build_question("A", "B", "C", "sentence"))
        self.assertIn("勉强", sent.call_args.args[1])
        self.assertIn("缺口：", sent.call_args.args[1])

    def test_semantic_output_cannot_omit_rows_or_label_jargon_noise(self):
        import semantic
        client = self.client("analyst")
        user = semantic.PROMPT % ("1. 压缩机\n2. 七十七", 2)
        with mock.patch.object(client, "_send", side_effect=[
                "1|speech|-", "1|speech|-\n2|speech|P"]) as sent:
            answer = client.chat(semantic.SYSTEM, user)
        self.assertEqual(2, sent.call_count)
        self.assertEqual({1: ("speech", False), 2: ("speech", True)}, semantic.parse(answer, 2))
        self.assertIn("专业词及短句均为speech", sent.call_args.args[1])

    def test_subtitle_grouping_and_fixed_count_are_validated(self):
        import sync_server
        client = self.client("analyst")
        user = "1: 0~1 你好\n2: 1~2 世界"
        with mock.patch.object(client, "_send", side_effect=[
                '{"groups":[[1],[1]],"reason":"重复"}',
                '{"groups":[[1,2]],"reason":"合并"}']) as sent:
            answer = client.chat(sync_server.POLISH_SYS, user)
        self.assertEqual([[1, 2]], json.loads(answer)["groups"])
        self.assertEqual(2, sent.call_count)
        with mock.patch.object(client, "_send", side_effect=[
                '{"fixed":["你好"]}', '{"fixed":["你好","世界"]}']) as sent:
            answer = client.chat(sync_server.CALIBRATE_SYS, "1 | 0~1 | 你好\n2 | 1~2 | 世界")
        self.assertEqual(2, len(json.loads(answer)["fixed"]))

    def test_proofread_contract_keeps_only_writable_numbered_rows(self):
        import proofread
        client = self.client("analyst")
        user = proofread.build_batch([("x", "压索机"), ("y", "完成了")],
                                     "压缩机|压索机", before=[("p", "只读前文")])
        with mock.patch.object(client, "_send", side_effect=[
                "1. 压缩机", "1. 压缩机\n2. 完成了"]):
            answer = client.chat("按输入合同续写", user)
        self.assertEqual(["压缩机", "完成了"], proofread.parse_batch(answer, 2))

    def test_alternate_subtitle_contract_keeps_global_pipe_numbers(self):
        client = self.client("editor")
        user = ("只输出「编号|修正后的文字」，一行一条。\n只读上文：前文\n"
                "待校对（只输出这些编号）：\n41|压索机\n42|完成了")
        with mock.patch.object(client, "_send", side_effect=[
                "1|压缩机\n2|完成了", "41|压缩机\n42|完成了"]) as sent:
            answer = client.chat("你是字幕校对员，只按格式输出。", user)
        self.assertEqual("41|压缩机\n42|完成了", answer)
        self.assertIn("视频素材分析员", sent.call_args.args[1])

    def test_remote_initialization_does_not_probe_local_gpu_models_or_ports(self):
        import proofread
        from ui.llm_bridge import LLM
        remote = self.client()
        with mock.patch("llm_gateway.OpenAICompatibleClient.from_config", return_value=remote), \
                mock.patch("llm_config.pick", side_effect=AssertionError("local model lookup")), \
                mock.patch("proofread._cuda_ok", side_effect=AssertionError("local GPU probe")), \
                mock.patch.object(LLM, "_free_port", side_effect=AssertionError("local port probe")):
            self.assertTrue(proofread.Server(task="paragraph").api.enabled)
            self.assertTrue(LLM(base_dir=os.getcwd()).api.enabled)

    def test_judge_json_array_is_preserved_for_existing_parser(self):
        import judge
        client = self.client("director")
        user = judge.PROMPT.format(goal="做机器", n=1, lines="1. (2.0秒) 接通电源")
        with mock.patch.object(client, "_send", return_value=
                               '[{"i":1,"keep":true,"rule":"4","why":"主要操作"}]'):
            self.assertIsInstance(json.loads(client.chat(judge.SYS, user)), list)

    def test_endpoint_and_failure_detail_redact_all_configured_credentials(self):
        client = self.client(base_url="https://url-user:url-password@host.test/v4?key=query-secret",
                             headers={"X-Private": "header-secret"})
        detail = ('bad https://url-user:url-password@host.test/v4?key=query-secret '
                  'Bearer private-key X-Private=header-secret '
                  '{"api_key":"extra-secret"}')
        safe = client._redact(detail)
        for secret in ("url-user", "url-password", "query-secret", "private-key",
                       "header-secret", "extra-secret"):
            self.assertNotIn(secret, safe)

    def test_http_error_response_stream_is_closed(self):
        client = self.client()
        stream = io.BytesIO(b'{"error":"bad model"}')
        error = urllib.error.HTTPError("https://host.test", 400, "bad", {}, stream)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(llm_gateway.APIRequestError):
                client.chat("contract", "source")
        self.assertTrue(stream.closed)

    def test_explicit_installation_roots_load_their_own_models_and_headers(self):
        import llm_config
        from ui.llm_bridge import LLM
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for root, model in ((first, "first-model"), (second, "second-model")):
                conf = os.path.join(root, "conf")
                os.makedirs(conf)
                with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as handle:
                    json.dump({"llm_provider": "zhipu", "llm_api_model": model,
                               "llm_api_key": model + "-key"}, handle)
                with open(os.path.join(conf, "llm.json"), "w", encoding="utf-8") as handle:
                    json.dump({"api": {"headers": {"X-Root": model}},
                               "disable_thinking": False}, handle)
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(llm_config, "APP_SETTINGS", os.path.join(second, "conf", "settings.json")):
                first_client = llm_gateway.OpenAICompatibleClient.from_config(first)
                second_client = llm_gateway.OpenAICompatibleClient.from_config(second)
                self.assertEqual("first-model", first_client.model)
                self.assertEqual({"X-Root": "first-model"}, first_client.headers)
                self.assertEqual("second-model", second_client.model)
                self.assertFalse(llm_config.load(base_dir=first)["disable_thinking"])
                self.assertEqual("first-model", LLM(base_dir=first).api.model)

    def test_explicit_empty_installation_cannot_inherit_other_directory_api(self):
        import llm_config
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as other:
            path = os.path.join(other, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"llm_provider": "deepseek", "llm_api_model": "other"}, handle)
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(llm_config, "APP_SETTINGS", path):
                self.assertTrue(llm_config.api_settings()["enabled"])
                self.assertFalse(llm_gateway.OpenAICompatibleClient.from_config(empty).enabled)


class FailureAndCacheTests(unittest.TestCase):
    def _failing_server(self):
        server = mock.Mock()
        server.chat.side_effect = llm_gateway.APIRequestError("HTTP 400: invalid model")
        return server

    def test_writer_batch_api_failure_is_not_a_missing_chunk(self):
        events = script_writer._source_events({"duration": 3,
                                               "segments": [[0, 2, "做机器"]]})
        with self.assertRaises(llm_gateway.APIRequestError):
            script_writer._read_batch_with_retry(
                self._failing_server(), {}, events, 1, lambda _msg: None)

    def test_invalid_api_token_does_not_trigger_batch_bisection(self):
        data = {"duration": 10, "segments": [[i * 2, i * 2 + 1, "做机器"] for i in range(4)]}
        events = script_writer._source_events(data)
        server = self._failing_server()
        server.chat.side_effect = llm_gateway.APIRequestError("HTTP 401: invalid API token")
        with self.assertRaises(llm_gateway.APIRequestError):
            script_writer._read_batch_with_retry(server, data, events, 1, lambda _msg: None)
        self.assertEqual(1, server.chat.call_count)

    def test_director_and_analyst_api_failures_do_not_produce_partial_results(self):
        import director
        import semantic
        with mock.patch("proofread.Server", return_value=contextlib.nullcontext(self._failing_server())):
            with self.assertRaises(llm_gateway.APIRequestError):
                director.plan({"segments": [[0, 2, "做机器"]]}, on_log=lambda _msg: None)
        with mock.patch("proofread.Server", return_value=contextlib.nullcontext(self._failing_server())):
            with self.assertRaises(llm_gateway.APIRequestError):
                semantic.analyze([[0, 2, "做机器"]], on_log=lambda _msg: None)

    def test_director_review_failure_is_not_swallowed(self):
        import director
        server = self._failing_server()
        server.chat.side_effect = ["1", llm_gateway.APIRequestError("review failed")]
        data = {"segments": [[0, 2, "目标"], [2, 4, "结果"]],
                "script": {"mainline": "搭建机器"}}
        with mock.patch("director._windows", return_value=[(0, 1)]), \
                mock.patch("proofread.Server", return_value=contextlib.nullcontext(server)):
            with self.assertRaises(llm_gateway.APIRequestError):
                director.plan_by_segment(data, on_log=lambda _msg: None)

    def test_longchain_and_index_api_failures_do_not_fall_back_to_rules(self):
        import longchain
        import index
        with self.assertRaises(llm_gateway.APIRequestError):
            longchain.judge("前文", "删除", "后文", server=self._failing_server())
        with mock.patch("semantic.analyze", side_effect=llm_gateway.APIRequestError("bad request")):
            with self.assertRaises(llm_gateway.APIRequestError):
                index.semantic_pass([[0, 2, "做机器"]])

    def test_continuity_result_is_not_overridden_by_negation_in_reason(self):
        import longchain
        server = mock.Mock()
        server.chat.return_value = "结果：通\n理由：没有断裂\n缺口：无"
        self.assertEqual("通", longchain.judge("前文", "删除", "后文", server=server)["result"])

    def test_legacy_auto_cut_worker_stops_after_remote_writer_failure(self):
        import cards_ui
        import autocut
        data = {"duration": 20, "analysis": {}, "segments": [[0, 2, "做机器"]],
                "script": {"cut_level": "medium", "keep_ratio": 0.75,
                           "mode": "gameplay", "sections": [{}], "backend_signature": "local"}}
        worker = cards_ui.AutoCutWorker("unused", "unused.mp4", edit_mode="gameplay")
        errors = []
        worker.err.connect(errors.append)
        with mock.patch("cards_ui.load_settings", return_value={"cut_keep_percent": 75, "keep_ratio": 0.75}), \
                mock.patch("autocut.load_index", return_value=data), \
                mock.patch("autocut.load_conf", return_value={}), \
                mock.patch("llm_gateway.backend_signature", return_value="api:test"), \
                mock.patch("script_writer.build", side_effect=llm_gateway.APIRequestError("remote failed")), \
                mock.patch("autocut.render") as render, \
                mock.patch("builtins.open", mock.mock_open()):
            worker.run()
        render.assert_not_called()
        self.assertTrue(errors and "APIRequestError" in errors[0])

    def test_legacy_retake_outer_handlers_cannot_swallow_remote_error(self):
        import autocut
        data = {"duration": 20, "segments": [[0, 2, "目标"], [4, 6, "结果"]]}
        conf = {"select": {"auto_dedupe": "0", "auto_retake": "1", "retake_llm": "1"}}
        with mock.patch("retake_pattern.find_aborted_attempts", return_value=[{"start": 0, "end": 3}]), \
                mock.patch("retakes_v2.adjudicate", side_effect=llm_gateway.APIRequestError("remote failed")):
            with self.assertRaises(llm_gateway.APIRequestError):
                autocut.apply_cut_level(data, [(0, 20)], conf, "medium")

    def test_remote_writer_empty_results_never_use_local_fallback_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"duration": 4, "segments": [[0, 2, "做机器"]]}, handle)
            with mock.patch("llm_gateway.backend_signature", return_value="api:test"), \
                    mock.patch("script_writer.read_pass", return_value=[]), \
                    mock.patch("script_writer.fallback_sections") as fallback:
                with self.assertRaises(llm_gateway.APIRequestError):
                    script_writer.build(path, on_log=lambda _msg: None)
            fallback.assert_not_called()

    def test_backend_signature_is_private_and_changes_with_account_or_model(self):
        settings = {"enabled": True, "provider": "zhipu", "protocol": "openai",
                    "base_url": "https://host.test/v4", "api_key": "account-a-secret",
                    "model": "model-a", "headers": {"X-Team": "private-routing"}}
        first = llm_gateway.backend_signature(settings)
        for key, value in (("model", "model-b"), ("api_key", "account-b-secret"),
                           ("headers", {"X-Team": "other-routing"})):
            changed = dict(settings, **{key: value})
            self.assertNotEqual(first, llm_gateway.backend_signature(changed))
        self.assertNotIn("secret", first)
        self.assertNotIn("routing", first)
        self.assertEqual("local", llm_gateway.backend_signature({"enabled": False}))

    def test_writer_does_not_reuse_local_or_different_api_cached_chunks(self):
        data = {"duration": 4, "segments": [[0, 2, "做机器"]]}
        import source_timeline
        revision = source_timeline.revision(script_writer._source_events(data))
        for old_backend in ("local", "api:old-model"):
            with self.subTest(old_backend=old_backend), tempfile.TemporaryDirectory() as tmp:
                fixture = copy.deepcopy(data)
                fixture["script"] = {"backend_signature": old_backend,
                                     "timeline_schema": source_timeline.SCHEMA,
                                     "timeline_revision": revision,
                                     "chunks": [{"summary": "old"}]}
                path = os.path.join(tmp, "index.json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(fixture, handle)
                with mock.patch("llm_gateway.backend_signature", return_value="api:new-model"), \
                        mock.patch("script_writer.read_pass", return_value=[]) as read:
                    with self.assertRaises(llm_gateway.APIRequestError):
                        script_writer.build(path, on_log=lambda _msg: None)
                read.assert_called_once()

    def test_index_api_labels_and_workflow_resume_are_separated(self):
        import index
        import autopilot
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "source.mp4")
            with open(src, "wb") as handle:
                handle.write(b"media")
            with mock.patch("llm_gateway.backend_signature", return_value="api:new"):
                freshness = index.index_freshness(
                    {"source": src, "ai_backend_signature": "api:old"}, src)
                first = autopilot._run_signature(src, tmp, "gameplay", "heavy", None,
                                                 False, False, None, "full_auto")
            with mock.patch("llm_gateway.backend_signature", return_value="api:other"):
                other = autopilot._run_signature(src, tmp, "gameplay", "heavy", None,
                                                 False, False, None, "full_auto")
        self.assertFalse(freshness["reusable"])
        self.assertEqual("ai_backend_changed", freshness["reason"])
        self.assertNotEqual(first, other)

    def test_api_failure_stops_workflow_before_edit_or_render(self):
        import autopilot
        import workflow_engine
        calls = []
        def analyze(ctx):
            calls.append("analyze")
            return workflow_engine.StageResult("INDEX", {"index_dir": "unused"})
        def writer(ctx):
            calls.append("writer")
            raise llm_gateway.APIRequestError("HTTP 400: model rejected")
        def forbidden(ctx):
            self.fail("API failure reached downstream stage " + ctx.stage.name)
        handlers = {name: forbidden for name in workflow_engine.STAGE_ORDER}
        handlers.update(analyze=analyze, writer=writer)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "writer.*APIRequestError"):
                autopilot.run_one(os.path.join(tmp, "unused.mp4"), output_dir=tmp,
                                  stage_handlers=handlers, on_log=lambda _msg: None)
        self.assertEqual(["analyze", "writer"], calls)


if __name__ == "__main__":
    unittest.main()
