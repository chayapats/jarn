# contrib — research and benchmarking tooling

Scripts here are **not** part of the shipped `jarn` package. They support
experiments and benchmarks maintained by contributors; dependencies (e.g.
Modal, SWE-bench) are not declared in `pyproject.toml`.

| Script | Purpose |
|--------|---------|
| [`swebench_modal.py`](swebench_modal.py) | Modal-hosted SWE-bench-lite harness-prompt A/B (needs `modal`, OpenRouter secret) |

Build a wheel before running the SWE-bench script:

```bash
uv build
modal run contrib/swebench_modal.py::check_main
modal run contrib/swebench_modal.py::ab_main
```
