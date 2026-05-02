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

## Directory layout

```
tests/
  *.xml                         # Test inputs — one LoST findService request per file
  regression/
    seed.py                     # One-time seeder (run once per new test, then commit)
    runner.py                   # Regression runner (run any time)
    golden/
      <name>.golden.xml         # Captured response for each test input
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

1. Drop a new XML request file in `tests/` (e.g. `tests/validate_17.xml`).
2. Seed just that file: `python -m tests.regression.seed --force validate_17`
3. Inspect `tests/regression/golden/validate_17.golden.xml` and confirm the response
   is what you expect.
4. Commit both files together: the input XML and its golden file.

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
2. Run `python -m tests.regression.seed --force` to regenerate all golden files.
3. Commit the updated golden files with a commit message that explains the behavioral change.
