# Verification

Do not record test counts, migration heads, or line-number evidence here; those values become stale. Derive them from the repository when needed.

## Automated checks

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
node --test tests/frontend/*.mjs
PYTHONPATH=. .venv/bin/python -m ruff check app tests
PYTHONPATH=. .venv/bin/alembic heads
```

Architecture constraints live in `tests/test_architecture_boundaries.py`. Add an executable assertion there when a policy can be checked reliably.

## Manual checks

- Exercise credential-backed Gemini workflows with masked logs and verify file cleanup.
- Exercise document storage against configured services and verify source/checkpoint identities.
- Verify cosmetic changes at desktop and narrow widths, including keyboard focus and reduced motion.
