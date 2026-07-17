# Export integrity and manifest semantics

Each written Markdown export receives a sibling `.manifest.json` by default.
The manifest provides machine-readable provenance and separates distinct kinds
of loss or transformation that should not be conflated.

## JSON parsing

- `parse_error_count`: source lines that remained unparseable and were omitted.
- `repaired_json_line_count`: otherwise-valid JSON lines recovered by narrowly
  escaping invalid backslashes inside JSON strings.
- `repaired_json_lines`: line numbers, original decoder errors, repair class,
  and repair count.

The repair routine never joins lines, invents braces, or modifies valid JSON
escape sequences.

## Event accounting

- recognized schemas produced export records;
- ignored schemas are known transport/context records with a documented reason;
- unknown schemas are retained in the manifest for compatibility auditing;
- `--strict-events` converts unknown schemas into a failing exit status.

## Truncation accounting

Source-runtime truncation markers mean Codex omitted content before the exporter
read the rollout. `--source-truncation annotate` makes that irrecoverability
explicit. Exporter-created trimming uses separate sentinels and counters.

The manifest distinguishes markers found in raw source lines, markers reached by
record extraction, and markers present in the selected rendered record set.

## Token counting

The manifest reports method, encoding, optional library/version, exactness for
the selected encoding, and special-token policy. Exactness does not include
hidden API framing or server-side accounting.

## Reconstructed history

`--live-context` records compactions and rollbacks applied and labels the result
as reconstructed. Full chronological `--mode thread` remains the authoritative
history export.
