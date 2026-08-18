# Diagrams

Every diagram in [`README.md`](../README.md) and [`ARCHITECTURE.md`](../ARCHITECTURE.md) has its
source here. The `.mmd` files are the originals — GitHub renders them in place, and they are
inlined verbatim into the documents as ```` ```mermaid ```` blocks. Rendered SVGs are in
[`svg/`](svg/) for use in slides and the write-up.

Nineteen diagrams. Nothing here is decorative. Each one answers a question a judge is likely to ask, and the
numbers in them come out of the code or the live cluster.

| # | Diagram | Type | Answers |
|---|---|---|---|
| 01 | [system architecture](01-system-architecture.mmd) | flowchart | How does telemetry become an action, and how does the loop close? |
| 02 | [memory hierarchy](02-memory-hierarchy.mmd) | flowchart | Four tiers — and what in the *database* enforces each lifetime? |
| 03 | [prevention pipeline](03-prevention-pipeline-sequence.mmd) | sequence | One prevention end to end, including every branch |
| 04 | [playbook lifecycle](04-playbook-lifecycle.mmd) | state | Birth, growth, mutation, merge, promotion, death |
| 05 | [data model](05-data-model-er.mmd) | ER | Nine tables, and why `playbooks` has no fitness column |
| 06 | [competition and tier gate](06-competition-and-tier-gate.mmd) | flowchart | Why Thompson sampling and not argmax |
| 07 | [provenance replay](07-provenance-replay.mmd) | sequence | `AS OF SYSTEM TIME` — and the bug that would make it a lie |
| 08 | [rollback semantics](08-rollback-semantics.mmd) | flowchart | Act, verify, undo — and why "flat" is never a win |
| 09 | [multi-region topology](09-multi-region-topology.mmd) | flowchart | What `REGIONAL BY ROW` and `LOCALITY GLOBAL` each buy |
| 10 | [changefeed idempotency](10-changefeed-idempotency.mmd) | sequence | At-least-once delivery, exactly-once execution |
| 11 | [embedding pipeline](11-embedding-pipeline.mmd) | flowchart | One ruler, or every distance is measured differently |
| 12 | [technology stack](12-technology-stack-mindmap.mmd) | mindmap | The whole surface on one page |
| 13 | [deployment architecture](13-deployment-architecture.mmd) | flowchart | What is actually running on AWS |
| 14 | [genealogy](14-genealogy.mmd) | flowchart | Three real families, read out of the seeded cluster |
| 15 | [security model](15-security-model.mmd) | flowchart | Trust boundaries and least privilege |
| 16 | [failure semantics](16-failure-semantics.mmd) | flowchart | What happens when things go wrong |
| 17 | [verification map](17-verification-map.mmd) | flowchart | Every claim, and the command that proves it |
| 18 | [demo shot map](18-demo-shot-map.mmd) | timeline | The three-minute demo, beat by beat |
| 19 | [CockroachDB × AWS integration](19-cockroachdb-aws-integration.mmd) | flowchart | Per agent: which AWS service runs it, and which CockroachDB capability it thinks with |

## Regenerating the SVGs

```bash
npm install -g @mermaid-js/mermaid-cli
for f in diagrams/*.mmd; do
  mmdc -i "$f" -o "diagrams/svg/$(basename "$f" .mmd).svg"
done
```

Rendered with mermaid-cli 11.12. If you edit a `.mmd`, re-render the SVG **and** update the
matching ```` ```mermaid ```` block in `README.md` / `ARCHITECTURE.md` — the documents inline the
source rather than linking to it, so a diagram renders on GitHub without a round trip to an
image host.
