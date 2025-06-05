import os
import asyncio
import dagger
from github import Github

async def run_review(repo: str, pr_number: int, token: str) -> None:
    async with dagger.Connection(dagger.Config(log_output=os.stdout)) as client:
        src = client.host().directory('.', exclude=['.git', '__pycache__'])
        result = await (
            client.container()
            .from_("python:3.12-slim")
            .with_exec(["pip", "install", "uv"])
            .with_directory("/src", src)
            .with_workdir("/src")
            .with_exec(["uv", "pip", "install", "-r", "requirements.txt"])
            .with_exec(["flake8"], skip_entrypoint=True)
            .stdout()
        )

    gh = Github(token)
    pull = gh.get_repo(repo).get_pull(pr_number)
    pull.create_issue_comment(f"Dagger review results:\n```\n{result}\n```")

if __name__ == '__main__':
    repo_env = os.environ.get('GITHUB_REPOSITORY')
    pr_env = os.environ.get('GITHUB_PR_NUMBER')
    token_env = os.environ.get('GITHUB_TOKEN')
    if not (repo_env and pr_env and token_env):
        raise SystemExit('GITHUB_REPOSITORY, GITHUB_PR_NUMBER and GITHUB_TOKEN must be set')
    asyncio.run(run_review(repo_env, int(pr_env), token_env))
