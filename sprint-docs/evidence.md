# Read-only judge evidence

`sprint.evidence` exposes one operator-held JSON packet through bounded MCP tools.
The caller decides which sections to reference externally; Sprint does not choose
research evidence or assign a grade.

```python
from sprint.evidence import overview, access

initial_context = overview(
    packet,
    external_sections={"workspace", "workspace_scans", "gpu_workspace"},
)
# Every other section, including the entire trace, remains intact.
page = access(packet, "read_evidence", {
    "reference": "packet#/workspace/files/0/text",
    "offset": 0,
    "limit": 12000,
})
```

Long strings in selected sections become exact JSON pointers, sizes and hashes.
The original packet is unchanged. Hashes and unread pointers are not substitutes
for reviewing the source. All content remains available through pagination.

Serve a private retained packet with:

```bash
python -m sprint.evidence /operator/evidence.json
```

The stdio server exposes only `read_evidence` and `list_evidence`. It resolves
pointers within that packet and accepts no filesystem paths, commands or URLs.
Reads return at most 16,000 content characters; listings return at most 100 entries.
The caller must keep the packet immutable/private, disable unrelated judge tools,
retain retrieval records, validate output citations against the original packet,
and respect the judge model's actual context capacity. There is no character-based
whole-packet rejection in this module.

The research integration was exercised on Runpod with a fresh, separate-UID Codex
judge. A saved run with approximately 2.27 million estimated input tokens used
approximately 221,000 tokens initially, preserving all 368 transcript events;
24 read-only evidence calls completed and the resulting citations validated.
Counts use `o200k_base` and are estimates, not a claimed model tokenizer mapping.

This fixes evidence transport, not grader calibration or collection coverage.
Skipped workspace files remain coverage gaps. No access tool can recover evidence
that was never captured.

## Checkpoint and recovery integration

The native Runpod research adapter still needs to join Sprint's existing
[checkpoint API](checkpoints.md). The recovery boundary is the immutable release,
learner and GPU files, native harness session, remaining budgets, and resource/RPC
records. It must not promise restoration of running processes or GPU memory.
This integration is outstanding; the evidence reader does not implement it.
