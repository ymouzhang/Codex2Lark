# Verify Feishu writes

An upstream success response does not prove the requested result exists.

After document creation or editing:

1. inspect the live resource;
2. check the title and required text fragments;
3. check protected or forbidden fragments when relevant;
4. check expected block/resource counts where supported;
5. report warnings from creation, edit, and verification;
6. never claim success when verification failed.

For Sheets, read formulas and typed values back. For Base, read the affected
records. For whiteboards, use the returned token and preview/query capability
when available.

If verification fails after a write, report a `verification_error` and the live
resource URL. Attempt only bounded, non-duplicating repair; do not create a new
document as a retry.
