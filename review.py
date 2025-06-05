#!/usr/bin/env python3
"""
Enhanced Dagger-based GitHub PR reviewer script with OpenAI feedback integration.
(Updated for openai>=1.0.0)
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict

import dagger
from github import Github, GithubException
from openai import OpenAI, AsyncOpenAI, APIError  # Use the new client classes
import requests


class PRReviewer:
    def __init__(self, repo: str, pr_number: int, token: str):
        self.repo = repo
        self.pr_number = pr_number
        self.token = token
        self.gh = Github(token)
        self.repo_obj = self.gh.get_repo(repo)
        self.pr = self.repo_obj.get_pull(pr_number)

        # Instantiate a synchronous OpenAI client for feedback
        self.openai_client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY")  # Must be set in the environment
        )

        # If you ever need an async client, you can do:
        # self.openai_async = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    async def run_code_analysis(self, client: dagger.Client) -> Dict[str, str]:
        """Run comprehensive code analysis using Dagger."""
        src = client.host().directory(
            ".", exclude=[
                ".git", "__pycache__", "*.pyc", ".pytest_cache",
                "node_modules", ".venv", "venv"
            ]
        )

        # Create pyproject.toml if it doesn't exist
        pyproject_content = """
[project]
name = "pr-review"
version = "0.1.0"
dependencies = [
    "flake8",
    "black",
    "mypy",
    "pylint",
    "bandit",
    "isort"
]
"""

        container = (
            client.container()
            .from_("python:3.12-slim")
            .with_exec(["sh", "-c", "apt-get update && apt-get install -y git"])
            .with_exec(["pip", "install", "uv", "dagger", "PyGithub", "openai"])
            .with_directory("/src", src)
            .with_workdir("/src")
        )

        # Install dependencies
        try:
            if Path("pyproject.toml").exists():
                container = container.with_exec(["uv", "sync", "--system"])
            elif Path("requirements.txt").exists():
                container = container.with_exec([
                    "uv", "pip", "install", "--system", "-r", "requirements.txt"
                ])
            else:
                container = container.with_new_file("/src/pyproject.toml", pyproject_content)
                container = container.with_exec(["uv", "sync", "--system"])
        except Exception as e:
            print(f"Warning: Dependency installation issue: {e}")

        results: Dict[str, str] = {}

        # Get list of Python files changed in PR
        changed_files = [
            f.filename for f in self.pr.get_files()
            if f.filename.endswith(".py") and f.status in ["added", "modified"]
        ]

        if not changed_files:
            return {"info": "No Python files to analyze"}

        # Run different analysis tools with better error handling
        analysis_commands = {
            "flake8": ["flake8", "--max-line-length=88"] + changed_files,
            "black": ["black", "--check", "--diff"] + changed_files,
            "mypy": ["mypy", "--ignore-missing-imports"] + changed_files,
            "bandit": ["bandit", "-f", "txt"] + changed_files,
            "isort": ["isort", "--check-only", "--diff"] + changed_files
        }

        for tool, cmd in analysis_commands.items():
            try:
                exec_container = container.with_exec(cmd)

                try:
                    result = await exec_container.stdout()
                    if result.strip():
                        results[tool] = result
                    else:
                        results[tool] = f"✅ {tool}: No issues found"
                except:
                    # If stdout fails, the command likely returned non-zero but produced output
                    try:
                        error_container = container.with_exec([
                            "sh", "-c", f"{' '.join(cmd)}; echo 'Exit code: '$?"
                        ])
                        error_result = await error_container.stdout()
                        results[tool] = f"⚠️ {tool}: {error_result}"
                    except:
                        results[tool] = f"⚠️ {tool}: Found issues but couldn't capture output"

            except Exception as e:
                results[tool] = f"❌ {tool}: Analysis failed - {str(e)}"

        return results

    def format_review_comment(self, results: Dict[str, str]) -> str:
        """Format analysis results into a readable review comment."""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        comment = f"""## 🤖 Automated Code Review

*Generated on {timestamp}*

### Analysis Results:

"""

        if "info" in results:
            comment += f"ℹ️ {results['info']}\n"
            return comment

        for tool, result in results.items():
            if result.startswith("✅"):
                comment += f"**{tool.upper()}**: {result}\n\n"
            elif result.startswith("❌"):
                comment += f"**{tool.upper()}**: {result}\n\n"
            else:
                snippet = result if len(result) < 1000 else result[:1000] + "\n... (truncated)\n"
                comment += f"**{tool.upper()}**:\n```\n{snippet}```\n\n"

        comment += """
---
*This review was generated automatically. Please review the suggestions and apply fixes as needed.*
        """
        return comment

    def create_line_comments(self, results: Dict[str, str]) -> None:
        """Create specific line comments for issues found (e.g., from flake8)."""
        if "flake8" in results and not results["flake8"].startswith("✅"):
            lines = results["flake8"].split("\n")
            commit_sha = self.pr.head.sha

            for line in lines:
                if ":" in line and not line.startswith("✅"):
                    try:
                        # Parse format: filename:line:col: error_code message
                        parts = line.split(":")
                        if len(parts) >= 4:
                            filename = parts[0]
                            line_num = int(parts[1])
                            message = ":".join(parts[3:]).strip()

                            try:
                                self.pr.create_review_comment(
                                    body=f"🔍 Linting issue: {message}",
                                    commit=self.repo_obj.get_commit(commit_sha),
                                    path=filename,
                                    line=line_num
                                )
                            except GithubException as e:
                                print(f"Could not create line comment: {e}")
                    except (ValueError, IndexError):
                        continue

    def generate_ai_feedback(self, results: Dict[str, str]) -> str:
        """
        Given the static-analysis results, call OpenAI’s Chat API to produce
        concise, actionable feedback on the code or issues found.
        """
        # Concatenate the analysis results into one string
        analysis_blob = ""
        for tool, output in results.items():
            truncated = (
                output if len(output) < 2000
                else output[:2000] + "\n... (truncated)\n"
            )
            analysis_blob += f"### {tool.upper()} Results:\n{truncated}\n\n"

        # Build ChatCompletion request via the new client
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert Python code reviewer. "
                            "Read the static-analysis results below and provide "
                            "concise, actionable feedback and suggestions."
                        )
                    },
                    {
                        "role": "user",
                        "content": (
                            f"{analysis_blob}\n\n"
                            "Please summarize the most important issues "
                            "and suggest improvements. Keep each suggestion brief."
                        )
                    }
                ],
                temperature=0.2,
                max_tokens=512
            )
            # Access response fields via attributes instead of dicts
            return response.choices[0].message.content.strip()

        except APIError as e:
            return f"❌ Could not generate AI feedback: {str(e)}"

    def fetch_pr_diff(self) -> str:
        """
        Download the raw unified diff for this PR via GitHub’s API.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3.diff"
        }
        resp = requests.get(self.pr.diff_url, headers=headers)
        resp.raise_for_status()
        return resp.text

    def generate_ai_feedback_on_diff(self, results: Dict[str, str]) -> str:
        """
        1) Fetch the PR diff.
        2) Build a prompt including the diff and optional static-analysis results.
        3) Call OpenAI to get high-level feedback on these changes.
        """
        # Fetch the raw diff
        try:
            diff_text = self.fetch_pr_diff()
        except Exception as e:
            return f"❌ Could not fetch PR diff: {e}"

        # Truncate diff if it's too large
        if len(diff_text) > 50000:
            diff_blob = diff_text[:50000] + "\n... (diff truncated)\n"
        else:
            diff_blob = diff_text

        # (Optional) Include static-analysis results for context
        analysis_blob = ""
        for tool, output in results.items():
            truncated = output if len(output) < 2000 else output[:2000] + "\n... (truncated)\n"
            analysis_blob += f"### {tool.upper()} Results:\n{truncated}\n\n"

        # Build system and user messages
        system_msg = {
            "role": "system",
            "content": (
                "You are an expert Python code reviewer. "
                "Below is a unified diff for a pull request. "
                "Review the changes and provide concise, actionable feedback:\n"
                "- Style and formatting comments\n"
                "- Potential bugs or edge cases\n"
                "- Suggestions for improvement\n"
            )
        }

        user_content = f"""```diff
{diff_blob}
```

{analysis_blob}

Please comment mainly on the **diff above**, referencing files or line numbers as needed."""
        user_msg = {"role": "user", "content": user_content}

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4",
                messages=[system_msg, user_msg],
                temperature=0.3,
                max_tokens=800
            )
            return response.choices[0].message.content.strip()

        except APIConnectionError as conn_exc:
            return f"❌ OpenAI connection error: {conn_exc}"
        except RateLimitError as rl_exc:
            return f"❌ OpenAI rate limit exceeded: {rl_exc}"
        except APIError as api_exc:
            return f"❌ OpenAI API error: {api_exc}"
        except Exception as e:
            return f"❌ Unexpected error generating AI feedback: {e}"

    async def run_review(self) -> None:
        """Run the complete review process: static analysis + AI feedback + GitHub comments."""
        print(f"Starting review for PR #{self.pr_number} in {self.repo}")

        try:
            async with dagger.Connection(dagger.Config(log_output=sys.stdout)) as client:
                # 1) Run static analysis
                results = await self.run_code_analysis(client)

                # 2) Save raw results to JSON (artifact)
                with open(f"review-{self.pr_number}.json", "w") as f:
                    json.dump(results, f, indent=2)

                # 3) Format the static-analysis comment
                main_comment = self.format_review_comment(results)

                # 4) Generate AI feedback on the diff
                ai_feedback = self.generate_ai_feedback_on_diff(results)
                main_comment += "\n\n---\n\n"
                main_comment += "## 🤖 AI-Generated Feedback on Diff\n\n"
                main_comment += ai_feedback

                # 5) Post the combined comment to GitHub
                self.pr.create_issue_comment(main_comment)

                # 6) Create line-specific comments for flake8 issues
                self.create_line_comments(results)

                print("✅ Review completed successfully!")

        except Exception as e:
            error_msg = f"❌ Review failed: {str(e)}"
            print(error_msg)
            try:
                self.pr.create_issue_comment(f"## 🚨 Review Error\n\n{error_msg}")
            except:
                print("Could not post error comment to PR")
            sys.exit(1)


def main():
    """Main entry point."""
    # Get required environment variables
    repo_env = os.environ.get("GITHUB_REPOSITORY")
    pr_env = os.environ.get("GITHUB_PR_NUMBER")
    token_env = os.environ.get("GITHUB_TOKEN")

    if not all([repo_env, pr_env, token_env]):
        print("❌ Missing required environment variables:")
        print(f"  GITHUB_REPOSITORY: {'✅' if repo_env else '❌'}")
        print(f"  GITHUB_PR_NUMBER: {'✅' if pr_env else '❌'}")
        print(f"  GITHUB_TOKEN: {'✅' if token_env else '❌'}")
        sys.exit(1)

    try:
        pr_number = int(pr_env)
    except ValueError:
        print(f"❌ Invalid PR number: {pr_env}")
        sys.exit(1)

    # Run the review
    reviewer = PRReviewer(repo_env, pr_number, token_env)
    asyncio.run(reviewer.run_review())


if __name__ == "__main__":
    main()