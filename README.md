# dagger-pr-review

This repository contains Dagger workflows to automate pull request reviews.

The `review.py` script uses the Dagger Python SDK with `uv` to lint your code and
comment on GitHub pull requests. Set the following environment variables and run
it with Python:

```bash
export GITHUB_REPOSITORY=jamesjellow/dagger-pr-review
export GITHUB_PR_NUMBER=SOME_INTEGER
export GITHUB_TOKEN=ghp_yourtoken
python3 review.py
```

The workflow lints the repository with `flake8` inside a container and posts the
output back to the pull request.

## GitHub Action

A GitHub Action at `.github/workflows/review.yml` runs the `review.py` workflow
whenever a pull request receives the `review` label. Label a PR with `review`
to trigger automatic linting and a comment with the results.