# Create a Feishu document

## Workflow

1. Identify the reader, their task, the document genre, and explicit constraints.
2. Build a coherent outline from the current conversation. Do not reproduce the
   chat transcript or invent unsupported facts.
3. Decide which relationships materially benefit from a native table,
   whiteboard, Sheet, or Base.
4. Create independent supporting artifacts first when the document needs their
   returned tokens.
5. Build a typed document specification and call `feishu_docs_publish`. Use
   `feishu_docs_create` only for user-supplied Markdown or an advanced XML block
   not yet represented by the typed compiler.
6. Create the document once. If a resource partially fails, repair the
   created document rather than creating duplicate documents.
7. Read the live document back and verify it.

## Writing style

- Lead with the outcome or decision.
- Use paragraphs for explanation and lists only for genuinely parallel items or
  steps.
- Keep heading levels consistent and do not mix numbering systems.
- Use callouts sparingly for a critical conclusion, warning, or decision.
- Keep a visual adjacent to the text it explains.
- Preserve citations and source links supplied in the conversation.
