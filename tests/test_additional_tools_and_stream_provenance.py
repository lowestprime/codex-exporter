"""Current Codex Desktop tool-catalog metadata and streaming provenance regression."""
import hashlib
import json
from pathlib import Path
from codex_exporter import cli
from codex_exporter.modern import stream_export

def test_additional_tools_registration_is_known_metadata():
    event={"type":"response_item","payload":{"type":"additional_tools","id":"abc","role":"assistant","tools":[{"type":"namespace","name":"test","description":"catalog","tools":[]}]}}
    assert cli.known_ignored_reason(event, False)=="tool_catalog_metadata"
    assert cli.classify_record(event,seq=2,include_actions=True,tool_outputs="full",max_tool_chars=20000,strip=False,max_line_chars=20000,max_repeated_lines=25) is None

def test_streamed_provenance_and_source_coverage(tmp_path):
    sid="01a0fecf-02ee-77d2-99a2-59e86e27cc9e"
    records=[
        {"type":"session_meta","payload":{"id":sid}},
        {"type":"response_item","payload":{"type":"additional_tools","id":"abc","role":"assistant","tools":[{"type":"namespace","tools":[]}]}},
        {"type":"event_msg","payload":{"type":"user_message","message":"Sample user question"}},
    ]
    raw=("\n".join(json.dumps(v) for v in records)+"\n").encode("utf-8")
    src=tmp_path/"sample.jsonl";src.write_bytes(raw)
    dest=tmp_path/"out.md"
    result=stream_export(src,dest,session_id=sid,title="ORCHESTRATOR",title_source="session_index",progress=False,unknown_events="report")
    m=json.loads(Path(result["manifest"]).read_text())
    markdown=dest.read_text()
    assert result["unknown_event_count"]==0
    assert m["records"]["source_lines"]==3
    assert m["records"]["ignored_tool_catalog_metadata"]==1
    assert m["title_source"]=="session_index"
    assert 'title_source: "session_index"' in markdown
    assert "ORCHESTRATOR" in markdown
    assert "Source JSONL records | 3" in markdown
    assert "Unrecognized event schemas | 0" in markdown
    assert hashlib.sha256(raw).hexdigest() in markdown
    assert m["source_sha256"]==hashlib.sha256(raw).hexdigest()
