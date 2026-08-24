# Route and create Feishu artifacts

## Native table vs Sheet vs Base

- Native document table: small, primarily read-only comparison or specification.
- Sheet: typed cells, formulas, filters, conditional formats, charts, or ongoing
  numerical work.
- Base: records with fields, views, status, assignees, attachments, or repeated
  operational updates.

Do not create an external artifact when a native table communicates the same
information more clearly.

## Whiteboards

Use a whiteboard for flow, sequence, dependency, topology, hierarchy, or causal
relationships. Use:

- Mermaid for standard diagrams and fast iteration;
- PlantUML for supported UML-oriented diagrams;
- SVG when deliberate visual layout is necessary.

Create/update the artifact, retain its returned token only in the active
conversation, and insert/reference it in the document. The plugin does not store
diagram source after the task.

## Ordering

Independent artifact creation may run concurrently. Writes that affect the same
document or workbook must be serialized and verified before dependent writes.
