# Contributing to DelayBahn

Thanks for your interest in contributing. Small, focused PRs are welcome. For
anything larger than a bug fix, please open an issue first so we can agree on
the approach before you write code.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest
```

## Pull request guidelines

- Keep the diff scoped to one change; unrelated cleanups belong in their own PR.
- Add or update tests under `tests/` for behavior changes.
- Be careful with anything that talks to bahn.de: the upstream is unofficial,
  rate-limited, and shared by all users of the site. Don't add code paths that
  multiply upstream traffic, and route requests through the existing client in
  `app/bahn_api.py` so its caching, request coalescing, and circuit breaker
  apply.
- Client-side code (scripts, MCP tools, etc.) must respect the public API rate
  limits — per IP: burst 10 requests / 10 s and 40 / minute on `/api/journeys` —
  and back off on HTTP 429 using the `Retry-After` header.

## Licensing of contributions

The project is licensed under CC BY-NC 4.0 (see `LICENSE`). So that the
project's licensing options stay open — including offering it under other terms
in the future — contributions are accepted under the MIT license: by opening a
pull request you agree that your contribution is licensed under MIT and may be
redistributed as part of the project under CC BY-NC 4.0 or any other terms
chosen by the maintainer. If that doesn't work for you, note it in the PR and
we'll discuss before merging.
