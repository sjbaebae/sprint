# Runtime limits and grading

```python
budget = Budget(
    seconds=3600,
    resource_requests=200,
    worker_concurrency=2,
    worker_spend=2.0,
)
train = Worker(seconds=600, lifetime_seconds=1800, hourly_cost=1.5)
```

Limits apply per trial. A unique admitted run or collection request consumes one
`resource_requests` slot, including malformed payloads and failed jobs. Duplicate
admitted IDs do not consume another slot. After exhaustion, requests receive
`ResourceRequestLimitError` without growing host lineage or provisioning workers;
one `request-limit.json` exhaustion record is retained. Rejection feedback lives
in the learner sandbox. Status reads do not consume request slots.

Independent workers may run concurrently. Requests to the
same persistent worker remain serialized. Checkpoints wait for the current batch
to drain and retain request IDs, completed jobs and spending reservations. Debug
resume does not reset those limits. `live.status()` reports remaining time, requests,
worker reservations and active worker names without changing the learner. File
snapshots do not restore processes.

`worker_spend` reserves each worker's **entire lifetime** at its operator-supplied
`hourly_cost` before accepting its first job. Reservations are not refunded after
failures. Missing rates fail before provisioning. This bounds declared worker
reservations, not the cloud invoice: model inference, learner/verifier resources,
image builds, storage, transfer and provider billing rounding are excluded.
Prices must use the same currency. No provider rates are guessed.

Modal learner and verifier sandboxes receive a provider-enforced lifetime of the
remaining trial time plus grading time plus 300 seconds. Workers receive their
own lifetime limit. Thus they expire even if the SSH host disappears. Reachable
host cancellation still attempts immediate cleanup. Other providers retain their
own lifecycle behavior; universal immediate cleanup after host loss is not yet
implemented. A provider TTL is an upper bound, not instant failure detection.

Network and hardware restrictions apply to restored containers as well as new
ones. The generic CLI harnesses do not yet provide framework-enforced action,
model-token or inference-dollar limits. Task instructions are not enforcement.
Remote interactive checkpoint control is still limited to running the live-run
Python driver on the selected host.

## Judge evidence

A validation bundle can use `context.grade_axes(criteria, evidence, expected_ids=["hypotheses", "design"], maximum=3)` to validate equal-weight 0–3 scores. Each criterion needs a unique ID,
integer score, concrete explanation and nonempty list of resolvable evidence IDs.
The judgments must cover exactly the author-declared criterion IDs; omitted or
invented axes fail grading. The operator builds the evidence index from trusted records supplied to the judge.
The helper returns a normalized component accepted by `judge(context, rubric)`.
Unresolvable references or malformed scores raise; the verifier quarantines errors.
This validates attribution, not whether the explanation warrants the score.

Confirmed deterministic violations disqualify; suspected violations quarantine.
Missing or failed grading never receives a fabricated fallback score.

Worker failures are terminal: subsequent requests cannot silently provision another
container using the original reservation. Closed worker state also survives resume.
Provisioning, code upload, execution and checkpoint collection obey the remaining
worker lifetime. Provider cleanup may take additional time after the deadline.

Worker teardown has a 60-second host timeout. Cleanup errors are retained in the
trusted worker journal, and the provider handle is retained for another cleanup
attempt. Campaign-final cleanup retries retained handles once. Unresolved failures
are written to `cleanup-errors.json` and shown in `review.html`, without discarding
completed trial results. A timeout is not recorded as successful resource deletion.

Final result collection also validates private grader output: only graded or
zero-reward disqualified results may contain a reward. Quarantined results have
none. Malformed JSON, unknown statuses and nonfinite/out-of-range rewards quarantine
that trial without discarding other campaign results. Disqualification requires
confirmed evidence; a graded result cannot contain unresolved violations.

A reference/oracle script exiting nonzero is an execution error, with its log and
exit code retained. This rule concerns the oracle entrypoint, not unsuccessful
experiments performed by an agent during otherwise valid research. Artifact-diff
or transcript-rendering failures are reported as visibility failures and do not
silently alter trusted grading.

Run `uv run python examples/sprint/failure/preflight.py` to verify reference failure
handling on Modal. It intentionally fails the reference and requires an unscored
execution error; an all-zero summary alone is not proof that this check passed.
