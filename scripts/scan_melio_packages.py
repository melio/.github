#!/usr/bin/env python3

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import base64
import requests

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
DEFAULT_PACKAGES = ["melio-crypto", "melio-db"]


def die(message: str, exit_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(exit_code)


def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GIT_TOKEN")
    if not token:
        die("Missing GitHub token in env (GITHUB_TOKEN/GH_TOKEN/GIT_TOKEN)")
    return token


def gh_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "melio-package-scanner"
    }


def list_repositories(owner: str, include_private: bool, include_archived: bool, session: requests.Session) -> List[Dict]:
    repos: List[Dict] = []

    # Try org repos first, then user repos as a fallback
    endpoints = [
        f"{GITHUB_API}/orgs/{owner}/repos",
        f"{GITHUB_API}/users/{owner}/repos",
    ]

    params = {
        "per_page": 100,
        "type": "all",
        "sort": "full_name",
        "direction": "asc",
    }

    for endpoint in endpoints:
        page = 1
        while True:
            resp = session.get(endpoint, params={**params, "page": page})
            if resp.status_code == 404 and endpoint.endswith("/orgs/" + owner + "/repos"):
                # Not an org, try user endpoint
                break
            resp.raise_for_status()
            page_repos = resp.json()
            if not isinstance(page_repos, list):
                break
            for repo in page_repos:
                if not include_archived and repo.get("archived"):
                    continue
                if not include_private and repo.get("private"):
                    continue
                repos.append(repo)
            if "next" not in resp.links:
                break
            page += 1
        if repos:
            break

    return repos


def find_package_json_paths(owner: str, repo: str, default_branch: str, session: requests.Session) -> List[str]:
    # Use the Git Trees API to list files in the repo recursively
    url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{default_branch}"
    params = {"recursive": 1}
    resp = session.get(url, params=params)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    tree = data.get("tree", [])
    paths = [item["path"] for item in tree if item.get("type") == "blob" and item.get("path", "").endswith("package.json")]
    return paths


def get_file_json(owner: str, repo: str, path: str, session: requests.Session) -> Optional[Dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    resp = session.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    # GitHub returns file content base64 encoded
    if data.get("encoding") == "base64":
        content = base64.b64decode(data.get("content", "").encode()).decode("utf-8", errors="replace")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def extract_versions(pkg_json: Dict, packages: List[str]) -> Dict[str, Optional[str]]:
    results: Dict[str, Optional[str]] = {pkg: None for pkg in packages}
    dependencies_sections = [
        pkg_json.get("dependencies", {}),
        pkg_json.get("devDependencies", {}),
        pkg_json.get("peerDependencies", {}),
        pkg_json.get("optionalDependencies", {}),
        pkg_json.get("bundledDependencies", {}),
    ]
    for section in dependencies_sections:
        if not isinstance(section, dict):
            continue
        for pkg in packages:
            if results[pkg] is None and pkg in section:
                results[pkg] = str(section[pkg])
    return results


def scan_repo(owner: str, repo: Dict, packages: List[str], session: requests.Session) -> Tuple[str, Dict[str, Optional[str]]]:
    repo_name = repo["name"]
    default_branch = repo.get("default_branch", "main")
    versions: Dict[str, Optional[str]] = {pkg: None for pkg in packages}

    paths = find_package_json_paths(owner, repo_name, default_branch, session)
    for path in paths:
        pkg_json = get_file_json(owner, repo_name, path, session)
        if not pkg_json:
            continue
        extracted = extract_versions(pkg_json, packages)
        for pkg in packages:
            if versions[pkg] is None and extracted.get(pkg):
                versions[pkg] = extracted[pkg]
        # Early exit if both found
        if all(versions[p] is not None for p in packages):
            break

    return repo_name, versions


def write_csv(rows: List[Tuple[str, Dict[str, Optional[str]]]], out_file: str, packages: List[str]) -> None:
    fieldnames = ["repository"] + packages
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for repo_name, versions in sorted(rows, key=lambda r: r[0].lower()):
            row = {"repository": repo_name}
            row.update({pkg: versions.get(pkg) or "" for pkg in packages})
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan all repos for melio package usage.")
    parser.add_argument("--owner", required=True, help="GitHub organization or username to scan")
    parser.add_argument("--out-file", required=True, help="Output CSV file path")
    parser.add_argument("--include-private", action="store_true", help="Include private repositories")
    parser.add_argument("--include-archived", action="store_true", help="Include archived repositories")
    parser.add_argument("--packages", nargs="*", default=DEFAULT_PACKAGES, help="Packages to scan for (default: melio-crypto melio-db)")

    args = parser.parse_args()

    token = get_token()
    session = requests.Session()
    session.headers.update(gh_headers(token))

    repos = list_repositories(args.owner, args.include_private, args.include_archived, session)
    if not repos:
        die(f"No repositories found for owner '{args.owner}'. Check token permissions and owner name.")

    results: List[Tuple[str, Dict[str, Optional[str]]]] = []
    for repo in repos:
        repo_name, versions = scan_repo(args.owner, repo, args.packages, session)
        results.append((repo_name, versions))

    write_csv(results, args.out_file, args.packages)
    print(f"Wrote {len(results)} rows to {args.out_file}")


if __name__ == "__main__":
    main()