"""Regression cases for capitalized Codex desktop item_completed records."""
from __future__ import annotations
import json
from pathlib import Path
from codex_exporter import cli
from codex_exporter.modern import stream_export


def obj(item: dict) -> dict:
    return {"timestamp": "2026-10-21T00:00:00Z", "type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": "11111111-1111-4111-8111-111111111111", "item": item}}


def classify(o: dict, *, summaries=False):
    return cli.classify_record(o, seq=1, include_actions=True, include_reasoning_summaries=summaries,
       tool_outputs="full", max_tool_chars=20000, strip=False, max_line_chars=20000, max_repeated_lines=25)


def test_substantive_items_keep_source_content_and_roles():
    user=obj({"type":"UserMessage","content":[{"type":"text","text":"USER exact message"}]})
    agent=obj({"type":"AgentMessage","content":[{"type":"text","text":"ASSISTANT exact message"}]})
    filechange=obj({"type":"FileChange", "status":"completed","changes":{"C:\\dir\\example.py":{"type":"add","content":"print('exact')\n"}}, "stdout":"Success", "stderr":""})
    extension=obj({"type":"Extension", "query":"find records", "results":[{"text":"extension evidence"}]})
    image=obj({"type":"ImageView","path":"C:\\images\\graph.png"})
    assert classify(user).role=="user" and "USER exact message" in classify(user).text
    assert classify(agent).role=="assistant" and "ASSISTANT exact message" in classify(agent).text
    assert "print('exact')" in classify(filechange).text and "C:\\dir\\example.py" in classify(filechange).text
    assert "extension evidence" in classify(extension).text
    assert "graph.png" in classify(image).text


def test_known_telemetry_compaction_reasoning_not_unknown():
    telemetry={"type":"token_usage_record","payload":{"usage":{"total_tokens":100}}}
    compaction=obj({"type":"ContextCompaction","id":"abc"})
    reasoning=obj({"type":"Reasoning","summary_text":["Explicit summary"],"raw_content":["HIDDEN RAW"]})
    assert classify(telemetry) is None
    assert cli.known_ignored_reason(telemetry, False)=="telemetry_token_count"
    assert cli.known_ignored_reason(compaction, False)=="context_history_event"
    assert classify(reasoning) is None
    assert cli.known_ignored_reason(reasoning, False)=="reasoning_summary_opt_in_disabled"
    summary=classify(reasoning,summaries=True)
    assert "Explicit summary" in summary.text and "HIDDEN RAW" not in summary.text


def test_streaming_known_items_not_unknown_sidecar(tmp_path):
    events=[obj({"type":"UserMessage","content":[{"text":"QUESTION"}]}),
      obj({"type":"AgentMessage","content":[{"text":"ANSWER"}]}),
      obj({"type":"FileChange","changes":{"a.py":{"type":"update","content":"CODEVALUE"}}}),
      obj({"type":"Reasoning","summary_text":[],"raw_content":[]}),
      {"type":"token_usage_record","payload":{"usage":{"total_tokens":10}}},
      {"type":"very_new_schema","payload":{"unfamiliar":"raw sidecar only"}}]
    source=tmp_path/'session.jsonl';source.write_text(''.join(json.dumps(x)+'\n' for x in events),encoding='utf-8')
    result=stream_export(source,tmp_path/'result.md',session_id='11111111-1111-4111-8111-111111111111',title='test',unknown_events='sidecar',progress=False)
    md=Path(result['file']).read_text(encoding='utf-8');man=json.loads(Path(result['manifest']).read_text())
    assert all(x in md for x in ['QUESTION','ANSWER','CODEVALUE'])
    assert man['records']['unknown_events']==1
    side=Path(man['unknown_sidecar']).read_text(encoding='utf-8')
    assert 'very_new_schema' in side and 'token_usage_record' not in side and 'Reasoning' not in side


def test_mcp_tool_completion_fallback_and_summary_opt_in(tmp_path):
    mcp=obj({"type":"McpToolCall","id":"tool-1","server":"codex_app", "tool":"open_in_codex",
             "arguments":{"target":{"type":"file","path":"C:\\safe\\note.md"}},
             "status":"completed","result":{"isError":False,"content":[{"type":"text","text":"queued"}]}})
    rec=classify(mcp)
    assert rec is not None and rec.kind=="action"
    assert "queued" in rec.text
    reasoning=obj({"type":"Reasoning","summary_text":["Explicit approved summary"],"raw_content":["PRIVATE ONLY"]})
    source=tmp_path/'events.jsonl';source.write_text(json.dumps(reasoning)+'\n',encoding='utf-8')
    res=stream_export(source,tmp_path/'out.md',session_id='11111111-1111-4111-8111-111111111111',
                      title='reasoning test',include_reasoning_summaries=True,unknown_events='sidecar',progress=False)
    text=Path(res['file']).read_text(encoding='utf-8')
    assert 'Explicit approved summary' in text and 'PRIVATE ONLY' not in text
