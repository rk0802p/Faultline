# Contributing to Faultline

Thank you for your interest in contributing to **Faultline**! We welcome contributions that help improve integration failure testing, resilience verification, and developer tooling.

---

## Architecture Principles

1. **Test outcomes, not just errors:** An injected failure is only meaningful when the resulting application-level business invariants are evaluated.
2. **Reproducibility by default:** Scenarios, seeds, event sequences, and assertion evaluations must be deterministic and inspectable.
3. **Zero runtime dependencies:** The core platform control plane, engine, SQLite WAL store, and CLI run on standard library Python 3.10+ without third-party pip dependencies.
4. **Local-first safety:** All network interactions default strictly to loopback (`127.0.0.1`, `localhost`, `::1`). Outbound calls to arbitrary external domains are rejected by security policy.
5. **No secret leakage:** Sensitive data (passwords, tokens, credentials, credit card numbers) are redacted before storage, display, or export.

---

## Local Development Workflow

### Prerequisites
- Python 3.10 or higher (Python 3.13 tested)
- Git
- Optional: Node.js (for validating web client scripts with `node --check`)
- Optional: Docker and Docker Compose (for multi-container verification)

### Setup
```bash
# Clone the repository
git clone https://github.com/rk0802p/Faultline.git
cd Faultline

# Verify Python syntax and bytecode compilation
python -m compileall -q faultline tests examples

# Validate web JavaScript syntax
node --check faultline/web/app.js
```

### Running Tests
All tests run with Python's built-in `unittest` framework:
```bash
# Run the entire test suite
python -m unittest discover -s tests -v

# Or using pytest (if installed)
python -m pytest tests/ -v
```

### Starting the Local Development Server
```bash
# Run server with embedded demo checkout and payment services
python -m faultline

# Open the dashboard
# http://127.0.0.1:8000
```

### CLI Verification
```bash
# Run an automated reliability benchmark across all seeded defects
python -m faultline.cli benchmark

# Execute a specific scenario against fixed or vulnerable implementations
python -m faultline.cli run --scenario payment-response-lost --implementation fixed
python -m faultline.cli run --scenario payment-response-lost --implementation vulnerable

# Check live operational metrics
python -m faultline.cli metrics
```

---

## Code Guidelines

- **Python standard library only for runtime:** Do not add third-party pip requirements to `pyproject.toml` runtime dependencies. Keep test/dev tools isolated.
- **Cross-platform encoding:** Avoid unescaped Unicode characters (such as `✓`, `•`, `→`) in console CLI printouts that may trigger `cp1252` encoding errors on Windows environments.
- **Preserve docstrings and comments:** Maintain documentation clarity and preserve existing architecture references.
- **Deterministic assertions:** Every seeded scenario must test verifiable business invariants (e.g. `no_duplicate_charge`, `idempotent_webhook_processing`, `charge_compensated_on_failure`).

---

## Submitting Pull Requests

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. Ensure all tests pass:
   ```bash
   python -m unittest discover -s tests -v
   ```
3. Commit with clear, descriptive commit messages.
4. Submit your pull request to `main`.
