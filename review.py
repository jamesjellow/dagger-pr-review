#!/usr/bin/env python3
"""
Enhanced Dagger-based GitHub PR reviewer script with OpenAI feedback integration.
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import dagger
from github import Github, GithubException
import openai


class PRReviewer:
    def __init__(self, repo: str, pr_number: int, token: str):
        self.repo = repo
        self.pr_number = pr_number
        self.token = token
        self.gh = Github(token)
        self.repo_obj = self.gh.get_repo(repo)
        self.pr = self.repo_obj.get_pull(pr_number)

        # Configure OpenAI API key
        openai.api_key = os.environ.get("OPENAI_API_KEY", "")

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
            .with_exec(["pip", "install", "uv", "openai", "dagger", "PyGithub"])
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
            truncated = output if len(output) < 2000 else output[:2000] + "\n... (truncated)\n"
            analysis_blob += f"### {tool.upper()} Results:\n{truncated}\n\n"

        # Build Chat messages
        messages = [
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
        ]

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=messages,
                temperature=0.2,
                max_tokens=512,
            )
            ai_reply = response.choices[0].message["content"].strip()
            return ai_reply

        except Exception as e:
            return f"❌ Could not generate AI feedback: {str(e)}"

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

                # 4) Generate AI feedback
                ai_feedback = self.generate_ai_feedback(results)
                main_comment += "\n\n---\n\n"
                main_comment += "## 🤖 AI-Generated Feedback\n\n"
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