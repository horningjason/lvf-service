# Contributing

## Getting Started

1. Fork the repository and clone your fork.
2. Create a feature branch: `git checkout -b my-feature`
3. Make your changes, commit, and push.
4. Open a pull request against `main`.

## Development Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
```

## Running Tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a pull request. New behavior should include new or updated tests.

## Code Style

- Follow existing conventions in the codebase.
- Keep functions focused and well-commented where the logic is non-obvious.
- No dead code or commented-out blocks in PRs.

## Submitting Changes

- One logical change per pull request.
- Write a clear PR description explaining *what* changed and *why*.
- Reference any related issues.

## Reporting Issues

Open a GitHub issue with:
- A clear description of the problem
- Steps to reproduce
- Expected vs. actual behavior
- Relevant log output or error messages

## Questions

Open a discussion or issue if you're unsure about something before investing time in an implementation.
