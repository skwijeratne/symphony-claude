# Tech Stack & Architecture

## Overview
This project is trying to implement a service that orchestrates coding agents to get project work done. The exact specification
is defined in SPEC.md . This is stand alone service / app which runs on a machine.

---

## Stack

### Frontend
- Currently None

### Backend
- **Framework**: TBD
- **Language**: Python 3.13

### Database
- **Primary**: PostgreSQL
- **ORM / query builder**: SQLAlchemy
- **Migrations**: Alembic
- **Caching**: Redis

### Infrastructure
- **Hosting**: e.g. Vercel, Railway, AWS
- **Containerisation**: e.g. Docker, Docker Compose
- **CI/CD**: e.g. GitHub Actions

---

## Folder Structure
```
project-root/
├── src/
│   ├── packages/
├── tests/
├── docs/
└── scripts/
```


---

## Coding Standards

### Naming Conventions
- Files/modules: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private methods/attributes: `_leading_underscore`
- Type aliases: `PascalCase`

### Code Style
- Formatter: Black
- Linter: Ruff
- Type checker: mypy
- Max line length: 88 (Black default)
- Docstrings: Google style

### Patterns
- Always use type hints on function signatures
- Use `dataclasses` or `pydantic` for data models, never plain dicts
- Prefer `pathlib.Path` over `os.path`
- Use `logging` module, never `print` for application output
- Always use `with` for file and resource handling
- Prefer list/dict comprehensions over loops where readable
- Use `Enum` for fixed sets of values, never plain strings
- Never use mutable default arguments (e.g. `def foo(items=[])`)

### Imports
- Order: stdlib → third party → local (enforced by Ruff)
- No wildcard imports (`from module import *`)
- Absolute imports only, no relative imports

### Error Handling
- Use specific exception types, never bare `except:`
- Always log exceptions before re-raising
- Define custom exceptions in `exceptions.py`

---

## Testing
- Framework: pytest
- Co-locate tests in `tests/` mirroring `src/` structure
- Prefix test files with `test_`
- Use fixtures over setUp/tearDown
- Mock external services, never hit real APIs in tests
- Coverage target: 80%

---
