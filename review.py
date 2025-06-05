#!/usr/bin/env python3
"""
Enhanced Dagger-based GitHub PR review script.
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import dagger
from github import Github, GithubException


class PRReviewer:
    def __init__(self, repo: str, pr_number: int, token: str):
        self.repo = repo
        self.pr_number = pr_number
        self.token = token
        self.gh = Github(token)
        self.repo_obj = self.gh.get_repo(repo)
        self.pr = self.repo_obj.get_pull(pr_number)
    
    async def run_code_analysis(self, client: dagger.Client) -> Dict[str, str]:
        """Run comprehensive code analysis using Dagger."""
        src = client.host().directory('.', exclude=[
            '.git', '__pycache__', '*.pyc', '.pytest_cache', 
            'node_modules', '.venv', 'venv'
        ])
        
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
            .with_exec(["apt-get", "update", "&&", "apt-get", "install", "-y", "git"], use_entrypoint=True)
            .with_exec(["pip", "install", "uv"])
            .with_directory("/src", src)
            .with_workdir("/src")
        )
        
        # Install dependencies
        try:
            # Try pyproject.toml first, then requirements.txt, then fallback
            if Path("pyproject.toml").exists():
                container = container.with_exec(["uv", "sync", "--system"])
            elif Path("requirements.txt").exists():
                container = container.with_exec(["uv", "pip", "install", "--system", "-r", "requirements.txt"])
            else:
                container = container.with_new_file("/src/pyproject.toml", pyproject_content)
                container = container.with_exec(["uv", "sync", "--system"])
        except Exception as e:
            print(f"Warning: Dependency installation issue: {e}")
        
        results = {}
        
        # Get list of Python files changed in PR
        changed_files = [f.filename for f in self.pr.get_files() 
                        if f.filename.endswith('.py') and f.status in ['added', 'modified']]
        
        if not changed_files:
            return {"info": "No Python files to analyze"}
        
        # Run different analysis tools
        analysis_commands = {
            "flake8": ["flake8"] + changed_files,
            "black": ["black", "--check", "--diff"] + changed_files,
            "mypy": ["mypy"] + changed_files,
            "bandit": ["bandit", "-r"] + changed_files,
            "isort": ["isort", "--check-only", "--diff"] + changed_files
        }
        
        for tool, cmd in analysis_commands.items():
            try:
                result = await container.with_exec(cmd, skip_entrypoint=True).stdout()
                if result.strip():
                    results[tool] = result
                else:
                    results[tool] = f"✅ {tool}: No issues found"
            except Exception as e:
                # Some tools return non-zero exit codes for issues found
                try:
                    # Try to get stderr which might contain the actual output
                    result = await container.with_exec(cmd, skip_entrypoint=True).stderr()
                    results[tool] = result if result.strip() else f"❌ {tool}: Command failed"
                except:
                    results[tool] = f"❌ {tool}: Analysis failed - {str(e)}"
        
        return results
    
    def format_review_comment(self, results: Dict[str, str]) -> str:
        """Format analysis results into a readable review comment."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        
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
                comment += f"**{tool.upper()}**:\n```\n{result[:1000]}{'...' if len(result) > 1000 else ''}\n```\n\n"
        
        comment += """
---
*This review was generated automatically. Please review the suggestions and apply fixes as needed.*
        """
        
        return comment
    
    def create_line_comments(self, results: Dict[str, str]) -> None:
        """Create specific line comments for issues found."""
        # Parse flake8 output for line-specific comments
        if "flake8" in results and not results["flake8"].startswith("✅"):
            lines = results["flake8"].split('\n')
            commit_sha = self.pr.head.sha
            
            for line in lines:
                if ':' in line and not line.startswith('✅'):
                    try:
                        # Parse format: filename:line:col: error_code message
                        parts = line.split(':')
                        if len(parts) >= 4:
                            filename = parts[0]
                            line_num = int(parts[1])
                            message = ':'.join(parts[3:]).strip()
                            
                            # Create review comment on specific line
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
    
    async def run_review(self) -> None:
        """Run the complete review process."""
        print(f"Starting review for PR #{self.pr_number} in {self.repo}")
        
        try:
            async with dagger.Connection(dagger.Config(log_output=sys.stdout)) as client:
                # Run code analysis
                results = await self.run_code_analysis(client)
                
                # Save results to file for artifacts
                with open(f"review-{self.pr_number}.json", "w") as f:
                    json.dump(results, f, indent=2)
                
                # Format and post review comment
                comment_body = self.format_review_comment(results)
                
                # Post main review comment
                self.pr.create_issue_comment(comment_body)
                
                # Create line-specific comments for critical issues
                self.create_line_comments(results)
                
                print("✅ Review completed successfully!")
                
        except Exception as e:
            error_msg = f"❌ Review failed: {str(e)}"
            print(error_msg)
            
            # Post error comment
            try:
                self.pr.create_issue_comment(f"## 🚨 Review Error\n\n{error_msg}")
            except:
                print("Could not post error comment to PR")
            
            sys.exit(1)


def main():
    """Main entry point."""
    # Get environment variables
    repo_env = os.environ.get('GITHUB_REPOSITORY')
    pr_env = os.environ.get('GITHUB_PR_NUMBER') 
    token_env = os.environ.get('GITHUB_TOKEN')
    
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


if __name__ == '__main__':
    main()