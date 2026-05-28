# LVF Regression Test Harness

## Philosophy: seed once, never rebuild

Golden files represent **known-good behavior locked in at a specific point in time**.
They are the ground truth the runner checks against. Once seeded, they must not be
regenerated unless you are deliberately resetting the baseline after an intentional,
reviewed behavior change.

**Do not re-run `seed.py` casually.** If you do, any bugs present at seed time become
the new baseline and the regression suite will no longer catch them. Treat golden files
as you would a signed test report: change them only with intent and a code review.

---

## Golden files are environment-local

Golden files are **not committed to the repository**. They are listed in `.gitignore`
and must be seeded locally before the runner will pass. This is intentional:

- A standalone LVF (no parent) produces different responses for out-of-coverage and
  recursion tests than one connected to a live parent LVF. Committing goldens from one
  environment would cause spurious failures in another.
- Golden files are a local baseline, not a shared artifact. Treat them as build output.

**First-time setup:** run `python -m tests.regression.seed` once after cloning to
establish your local baseline. Do not commit the generated files.

**After a deliberate behavior change:** run `python -m tests.regression.seed --force`
(or `--force <name>`) only for the tests that are intentionally affected. Never
force-reseed the entire suite as a way to silence failures — diagnose them first.

---

## Directory layout

```
tests/
  *.xml                         # Test inputs — one LoST findService request per file
  regression/
    seed.py                     # One-time seeder (run once per new test, do not commit output)
    runner.py                   # Regression runner (run any time)
    golden/
      .gitkeep                  # Keeps the directory in the repo; golden files are gitignored
      <name>.golden.xml         # Generated locally — not committed
```

---

## Running the tests

```powershell
# Run all tests
python -m tests.regression.runner

# Run a single test by name (XML file stem)
python -m tests.regression.runner --test validate_2
```

Exit code is `0` if all tests pass, `1` if any fail or any golden file is missing.

---

## Seeding golden files (run once only)

```powershell
# Seed all test inputs (skips any that already have a golden file)
python -m tests.regression.seed

# Force-overwrite all golden files (use only after a deliberate behavior change)
python -m tests.regression.seed --force

# Seed a single new test by name (safe — will not touch existing golden files)
python -m tests.regression.seed --force validate_17
```

`seed.py` refuses to overwrite existing golden files unless `--force` is passed.
This prevents accidental resets.

---

## Adding a new test

1. Drop a new XML request file in `tests/requests/` (e.g. `tests/requests/G2-SSAP-NEW-001.xml`).
2. Seed just that file: `python -m tests.regression.seed --force G2-SSAP-NEW-001`
3. Inspect `tests/regression/golden/G2-SSAP-NEW-001.golden.xml` and confirm the response
   is what you expect.
4. Commit only the input XML. The golden file is environment-local and is gitignored.

---

## What the runner compares

Comparison is **semantic** (parsed XML), not a string diff. Whitespace and attribute
ordering differences do not cause failures. The runner checks:

- **Outcome type** — `locationValidation`, `notFound`, `locationInvalid`, or
  `serviceNotImplemented`
- **`<valid>` list** — set of element QNames reported as valid (order-independent)
- **`<invalid>` value** — the single element QName reported as invalid (stop-on-first rule)
- **`<unchecked>` list** — set of element QNames reported as unchecked (order-independent)
- **`mapping sourceId`** — only compared when the golden file recorded a non-null value

---

## Resetting the baseline (intentional only)

If a deliberate algorithm or data change produces different correct responses:

1. Review and approve the new behavior in code review.
2. Run `python -m tests.regression.seed --force` (or `--force <name>`) to regenerate
   the affected golden files locally.
3. Golden files are gitignored — nothing to commit. The behavioral change is captured
   by the code diff alone.
