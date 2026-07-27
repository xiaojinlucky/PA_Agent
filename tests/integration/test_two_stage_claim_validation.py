"""WO-F：声明校验失败必须成为可恢复的结构化 partial record。"""
from __future__ import annotations

import copy
from unittest.mock import MagicMock

from pa_agent.ai.router import route_strategy_files
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken, OrchestratorEvent
from tests.fixtures.validators import schema_test_validator

from .conftest import VALID_STAGE1, VALID_STAGE2, make_reply


def _orchestrator(client, assembler, pending_writer, exp_reader):
    return TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=schema_test_validator(),
        pending_writer=pending_writer,
        exp_reader=exp_reader,
    )


def test_stage1_claim_failure_is_durable_and_has_stable_code(
    frame,
    pending_writer,
    assembler,
    exp_reader,
):
    bad_stage1 = copy.deepcopy(VALID_STAGE1)
    bad_stage1["support_levels"] = ["9999.0"]

    client = MagicMock()
    client.stream_chat.return_value = make_reply(bad_stage1)
    events: list[OrchestratorEvent] = []
    record = _orchestrator(
        client,
        assembler,
        pending_writer,
        exp_reader,
    ).submit(frame, CancelToken(), events.append)

    assert record.exception is not None
    assert record.exception["type"] == "claim_validation"
    assert record.exception["code"] == "price_out_of_range"
    assert record.analysis_price_tick == "0.1"
    pending_writer.save_partial_durable.assert_called_once()
    pending_writer.save_partial.assert_not_called()
    assert OrchestratorEvent.Stage2Started not in events
    assert client.stream_chat.call_count == 1


def test_stage2_claim_failure_is_durable_and_does_not_retry(
    frame,
    pending_writer,
    assembler,
    exp_reader,
):
    bad_stage2 = copy.deepcopy(VALID_STAGE2)
    bad_stage2["decision"]["take_profit_price_2"] = 9999.0

    client = MagicMock()
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        make_reply(bad_stage2),
    ]
    events: list[OrchestratorEvent] = []
    record = _orchestrator(
        client,
        assembler,
        pending_writer,
        exp_reader,
    ).submit(frame, CancelToken(), events.append)

    assert record.exception is not None
    assert record.exception["type"] == "claim_validation"
    assert record.exception["stage"] == "stage2"
    assert record.exception["code"] == "price_out_of_range"
    pending_writer.save_partial_durable.assert_called_once()
    pending_writer.save_partial.assert_not_called()
    assert OrchestratorEvent.Stage2Failed in events
    assert client.stream_chat.call_count == 2
