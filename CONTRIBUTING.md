# Contributing

## Before You Start

This service is a normative implementation of the LVF algorithm as specified in
`LVF_Algorithm_Specification_v53.docx`. Every gate decision, element ordering, and fallthrough
rule traces back to that document. **Read the relevant spec sections before changing any gate
or algorithm logic.** Changes that contradict the spec will be rejected regardless of test
coverage.

For development setup and running the service, see the [Quick Start — Python](README.md#quick-start--python) section of the README.

## Running and Adding Tests

The regression suite compares each request in `tests/requests/` against a golden file.

```powershell
# Run all tests
python -m tests.regression.runner

# Run one test by name
python -m tests.regression.runner --test G2-SSAP-VALID-002
```

**Adding a new test:**

1. Add a `tests/requests/<TEST-ID>.xml` file following the naming convention in `CLAUDE.md`.
2. Verify the expected behavior manually against the spec.
3. Seed the golden file: `python -m tests.regression.seed --force <TEST-ID>`
4. Commit both the request XML and the golden file together.

Do not run `python -m tests.regression.seed --force` without `--test` unless you intend to
reset the entire baseline after a deliberate behavior change.

All tests must pass before submitting a pull request. New behavior requires new or updated tests.

## Code Conventions

- **Cite the spec, don't describe the code.** If a comment is needed, reference the spec section
  (e.g., `# INF-027 §2.5.8`). Well-named identifiers already describe what the code does.
- **No fuzzy matching, no field mapping.** GIS field names are used verbatim per STA-006.3.
  All element comparisons are case-insensitive exact string match. Do not introduce partial or
  approximate matching.
- **Element hierarchy ordering is fixed.** The 33-position sequence in `models.ELEMENT_HIERARCHY`
  defines evaluation order. Changes require spec justification.
- **Stop-on-first-invalid is absolute.** Gate 2 places at most one element in `<invalid>`.
- **HNO on RCL is always `<unchecked>`.** This is an INF-027 requirement, not an oversight.

## Submitting Changes

- One logical change per pull request.
- Describe what changed, why, and which spec section governs it (for algorithm changes).
- If the change affects response output, include re-seeded golden files in the same commit with
  an explanation of why the output changed.
- Reference any related issues.

## Reporting Issues

Open a GitHub issue with:

- A clear description of the problem
- The request XML that triggers it (anonymise any real addresses)
- Expected vs. actual response XML
- Relevant log output — `LVF_LOG_LEVEL=DEBUG` captures full gate decisions
- The spec section you believe is violated, if applicable

## Questions

Open a discussion or issue before investing significant time in an implementation, particularly
for anything touching gate logic or element ordering. Misaligned assumptions are much cheaper
to catch early.
