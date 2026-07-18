# Anonymized regression corpus

These are synthetic, non-user-derived fixtures that model distinct Codex storage generations and control-flow events:

- `legacy-response-item.jsonl`: response-item message schema;
- `compaction-rollback.jsonl`: replacement-history compaction and rollback semantics;
- `../sample-session.jsonl`: event-message schema, command/output pairing, source truncation, explicit reasoning summary, and a narrowly repairable invalid Windows-path escape.

Add a minimized synthetic fixture whenever a new Codex version introduces a materially different event shape. Never commit private rollouts, prompts, paths, secrets, or raw user session content.
