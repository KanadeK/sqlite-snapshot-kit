# Contributing

Open an issue with the use case before proposing a large change. Keep the CLI offline and
runtime dependencies empty. Never include real HAR captures, production databases, private
archives or credentials in issues or tests. Use synthetic fixtures.

```sh
python -m venv .venv
# Activate .venv using your platform's instructions in README.md.
python -m pip install -e .
python -m pip install ruff==0.14.10
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Add a regression test that demonstrates a meaningful failure. Document behavior changes,
error codes and limits. See docs/DESIGN.md for the current acceptance criteria.
GitHub Actions only checks changes; it never writes commits or updates dependencies.
The maintainer reviews suggestions and authors any accepted repository changes.
