# Release runtime

Python is pinned to **3.10.13** in `.python-version`. Render's `PYTHON_VERSION`
overrides that file, so set it to the same exact value on both web and cron.
`sh scripts/release/build.sh` rejects any different interpreter before installing.

`requirements.in` is the reviewed direct production input. It intentionally keeps
the validated application dependency family, including FastAPI 0.129.0 and
Starlette 0.52.1 (route registry and TestClient compatibility), SQLAlchemy 2.0.54,
psycopg 3.3.6, and cryptography 46.0.7. Botocore is direct because application
modules import its exceptions. Httpx is production code's email/Square transport.
Pytest is not a production dependency.

`requirements.txt` is a universal, hash-locked resolver output, not a dump of the
workstation environment. It includes dependency edges and platform markers,
including SQLAlchemy's Linux greenlet requirement. Transitives were resolved
from the input; unrelated workstation packages were excluded. Both services use
this same lock. All artifacts install as wheels with hash validation.

`requirements-test.in` layers pytest 9.0.2 onto that exact production resolution;
`requirements-test.txt` locks the combined environment. Install production first,
then the test lock, and run `python -m pip check`. The production version set must
remain unchanged by the test installation.

Lock generation uses **uv 0.8.22**, Python 3.10.13:

```sh
python -m pip install uv==0.8.22
uv pip compile --universal --python-version 3.10.13 --generate-hashes --no-header requirements.in -o requirements.txt
uv pip compile --universal --python-version 3.10.13 --generate-hashes --no-header requirements-test.in -o requirements-test.txt
```

Do not regenerate locks during Render builds. Dependency/runtime upgrades are
separate reviewed changes requiring full tests and another recovery rehearsal.
The native pip installer bundled with the clean local 3.10.13 venv was 23.0.1;
it successfully installed both hash locks. Resolver tooling is not installed in
production and cannot silently re-resolve production versions during a build.

A fresh macOS Python 3.10.13 environment was built from these files. The identical
production lock was also installed to an isolated Linux x86-64 target with uv,
verifying Linux wheel availability and hashes (this is not Linux test execution).
The canonical full suite and cross-process recovery rehearsal use the fresh
release environment. See the canonical release-readiness record for totals.
