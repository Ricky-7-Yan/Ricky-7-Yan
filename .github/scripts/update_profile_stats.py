#!/usr/bin/env python3
"""Refresh the native Markdown profile statistics from GitHub GraphQL."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any


API_URL = "https://api.github.com/graphql"
LOGIN = "Ricky-7-Yan"
START_MARKER = "<!-- profile-stats:start -->"
END_MARKER = "<!-- profile-stats:end -->"

PROFILE_QUERY = """
query ProfileStats($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(
      first: 100
      after: $cursor
      privacy: PUBLIC
      ownerAffiliations: OWNER
    ) {
      totalCount
      nodes {
        isFork
        stargazerCount
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
    repositoriesContributedTo(
      first: 1
      includeUserRepositories: false
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) {
      totalCount
    }
    contributionsCollection {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
  }
}
"""

REPOSITORIES_QUERY = """
query Repositories($login: String!, $cursor: String!) {
  user(login: $login) {
    repositories(
      first: 100
      after: $cursor
      privacy: PUBLIC
      ownerAffiliations: OWNER
    ) {
      nodes {
        isFork
        stargazerCount
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


def graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Ricky-7-Yan-profile-stats",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode(errors="replace")
        raise RuntimeError(f"GitHub GraphQL request failed: {error.code} {details}") from error

    if result.get("errors"):
        raise RuntimeError(f"GitHub GraphQL returned errors: {result['errors']}")
    return result["data"]


def calculate_streaks(days: list[dict[str, Any]]) -> tuple[int, int]:
    today = date.today()
    ordered = sorted(
        (day for day in days if date.fromisoformat(day["date"]) <= today),
        key=lambda item: item["date"],
    )
    longest = 0
    running = 0
    for day in ordered:
        if day["contributionCount"] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0

    # A zero on the latest calendar day does not end a streak that ran through
    # the previous day, but older gaps do.
    current = 0
    cursor = len(ordered) - 1
    if cursor >= 0 and ordered[cursor]["contributionCount"] == 0:
        cursor -= 1
    while cursor >= 0 and ordered[cursor]["contributionCount"] > 0:
        current += 1
        cursor -= 1
    return current, longest


def fetch_stats(token: str) -> dict[str, int]:
    data = graphql(token, PROFILE_QUERY, {"login": LOGIN, "cursor": None})
    user = data["user"]
    if user is None:
        raise RuntimeError(f"GitHub user not found: {LOGIN}")

    repositories = user["repositories"]
    repository_nodes = list(repositories["nodes"])
    page_info = repositories["pageInfo"]
    while page_info["hasNextPage"]:
        page = graphql(
            token,
            REPOSITORIES_QUERY,
            {"login": LOGIN, "cursor": page_info["endCursor"]},
        )["user"]["repositories"]
        repository_nodes.extend(page["nodes"])
        page_info = page["pageInfo"]

    contributions = user["contributionsCollection"]
    calendar = contributions["contributionCalendar"]
    days = [
        day
        for week in calendar["weeks"]
        for day in week["contributionDays"]
    ]
    current_streak, longest_streak = calculate_streaks(days)

    return {
        "stars": sum(
            repository["stargazerCount"]
            for repository in repository_nodes
            if not repository["isFork"]
        ),
        "public_repos": repositories["totalCount"],
        "contributions": calendar["totalContributions"],
        "commits": contributions["totalCommitContributions"],
        "pull_requests": contributions["totalPullRequestContributions"],
        "issues": contributions["totalIssueContributions"],
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "contributed_repos": user["repositoriesContributedTo"]["totalCount"],
    }


def render(stats: dict[str, int]) -> str:
    value = lambda key: f"{stats[key]:,}"
    return "\n".join(
        [
            START_MARKER,
            "<!-- Updated automatically from the GitHub GraphQL API. -->",
            "| ⭐ Owned repo stars | 📦 Public repos | 🟩 Contributions (1 year) |",
            "| :---: | :---: | :---: |",
            f"| **{value('stars')}** | **{value('public_repos')}** | **{value('contributions')}** |",
            "",
            "| 💾 Commits (1 year) | 🔀 Pull requests (1 year) | 💬 Issues (1 year) |",
            "| :---: | :---: | :---: |",
            f"| **{value('commits')}** | **{value('pull_requests')}** | **{value('issues')}** |",
            "",
            "| 🔥 Current streak | 🏆 Longest streak (1 year) | 🤝 External repos contributed to |",
            "| :---: | :---: | :---: |",
            f"| **{value('current_streak')} days** | **{value('longest_streak')} days** | **{value('contributed_repos')}** |",
            END_MARKER,
        ]
    )


def update_readme(readme_path: Path, block: str) -> bool:
    original = readme_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )
    updated, replacements = pattern.subn(block, original)
    if replacements != 1:
        raise RuntimeError(
            f"Expected exactly one generated stats block, found {replacements}"
        )
    if updated == original:
        return False
    readme_path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN or GH_TOKEN is required", file=sys.stderr)
        return 1

    readme_path = Path(__file__).resolve().parents[2] / "README.md"
    changed = update_readme(readme_path, render(fetch_stats(token)))
    print("README.md updated" if changed else "README.md is already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
