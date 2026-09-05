---
name: release
description: Cut a release of ai-terminal-manager — bump the version in all three places, verify, tag, and let the publish workflow push to PyPI via trusted publishing. Use when asked to release, publish, bump the version, or push a new version to PyPI.
---

# Release

Publishing is automatic: pushing a `v*` tag runs `.github/workflows/publish.yml` (build → PyPI trusted publishing,
no tokens in the repo). Your job is to get `main` into a releasable state and push the tag.

## 1. Version — three places, one commit

```
pyproject.toml            version = "X.Y.Z"
src/atm/__init__.py       __version__ = "X.Y.Z"
uv.lock                   [[package]] name = "ai-terminal-manager" / version = "X.Y.Z"   (root package only)
```

Edit `uv.lock` by hand for the version line only. Do **not** run `uv lock` to regenerate it unless dependencies
changed — on machines with a mirror configured it rewrites every index URL.

Semver: docs-only / link fixes → patch; new subcommand or flag → minor; breaking CLI or config change → major.

## 2. Verify

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen pytest
uv build
python -c "import zipfile,glob; print(zipfile.ZipFile(glob.glob('dist/*.whl')[-1]).namelist())"   # atm/ only
python -c "import tarfile,glob; print(sorted({m.name.split('/',1)[1].split('/')[0] for m in tarfile.open(glob.glob('dist/*.tar.gz')[-1]).getmembers() if '/' in m.name}))"   # no research/
```

README links must be **absolute** (`https://github.com/lyfuci/ai-terminal-manager/blob/main/...`): PyPI renders
`README.md` as the project description and relative links 404 there. Check: `grep -nE '\]\([^)h#]' README.md` → empty.

## 3. Ship

The version bump goes through a PR like any change (three-section body). After it merges:

```bash
git checkout main && git pull --ff-only
git tag -a vX.Y.Z -m "vX.Y.Z — <one line>"
git push origin vX.Y.Z
gh run watch $(gh run list --workflow publish --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
curl -s https://pypi.org/pypi/ai-terminal-manager/X.Y.Z/json | python -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"
```

A PyPI release's description is immutable; a README mistake needs a new patch release, not a re-upload.

## 4. Afterwards

- Delete the merged branch (local + remote).
- Mirrors (aliyun, tuna) lag PyPI by minutes to an hour; `uv tool install ai-terminal-manager` through a mirror may
  fail briefly. `uv tool install git+https://github.com/lyfuci/ai-terminal-manager` works immediately.
- The installed tool is named `ai-terminal-manager` (PyPI `atm` was taken); the command is still `atm`.
  Upgrading from the pre-rename install: `uv tool uninstall atm` first.
