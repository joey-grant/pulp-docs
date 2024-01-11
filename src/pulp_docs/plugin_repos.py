"""
The classes representing plugins git/git-hub repositories.

Their purpose is to facilitate declaring and downloading the source-code.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import typing as t
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("mkdocs")

FIXTURE_WORKDIR = Path("tests/fixtures").absolute()
DOWNLOAD_CACHE_DIR = Path(tempfile.gettempdir()) / "repo_downloads"
RESTAPI_TEMPLATE = "https://docs.pulpproject.org/{}/restapi.html"


@dataclass
class RepoStatus:
    """
    Usefull status information about a downloaded repository.
    """

    download_source: t.Optional[str] = None
    use_local_checkout: bool = False
    has_readme: bool = True
    has_changelog: bool = True
    has_staging_docs: bool = True
    using_cache: bool = False
    original_refs: t.Optional[str] = None # as defined in repolist.yml

@dataclass
class Repo:
    """
    A git/gh repository representation.

    The real repository content is plugin sourcecode and markdown documentation.
    """

    title: str
    name: str
    owner: str = "pulp"
    branch: str = "main"
    local_basepath: t.Optional[Path] = None
    status: RepoStatus = RepoStatus()
    type: t.Optional[str] = None

    @property
    def rest_api_link(self):
        return RESTAPI_TEMPLATE.format(self.name)

    def download(self, dest_dir: Path, clear_cache: bool = False) -> str:
        """
        Download repository source from url into the {dest_dir} Path.

        Uses local in the following cases and order (else, downloads from github):
        - local_basepath is explicitly set
        - parent directory contain dir with self.name

        For remote download, uses GitHub API to get latest source code:

        Args:
            dest: The destination directory where source files will be saved.
                e.g /tmp/pulp-tmp/repo_sources/pulpcore
            clear_cache: Whether the cache should be cleared before downloading.
        Returns:
            The download url used
        """
        log.info("Downloading '{}' to '{}'".format(self.name, dest_dir.absolute()))

        if clear_cache is True:
            log.info("Clearing cache dir")
            shutil.rmtree(DOWNLOAD_CACHE_DIR, ignore_errors=True)
            DOWNLOAD_CACHE_DIR.mkdir()

        cached_repo = Path(DOWNLOAD_CACHE_DIR / self.name).absolute()
        download_from = cached_repo
        copy_path = cached_repo
        log_header = ""

        # from local filesystem
        if self.local_basepath is not None:
            log_header = "Using local checkout"
            download_from = Path(self.local_basepath / self.name).absolute()
            copy_path = download_from
        # from cache
        elif cached_repo.exists():
            log_header = "Using cache in tmpdir"
            download_from = cached_repo
            copy_path = cached_repo
            self.status.using_cache = True
        # from remote
        elif not cached_repo.exists():
            log_header = "Downloading from remote"
            download_from = download_from_gh_main(
                DOWNLOAD_CACHE_DIR / self.name, self.owner, self.name, self.branch
            )
            copy_path = DOWNLOAD_CACHE_DIR / self.name

        # copy from source/cache to pulp-docs workdir
        log.info(f"{log_header}: source={download_from}, copied_from={copy_path}")
        shutil.copytree(
            copy_path,
            dest_dir,
            ignore=shutil.ignore_patterns("tests", "*venv*", "__pycache__"),
        )

        self.status.download_source = str(download_from)
        return self.status.download_source


def download_from_gh_main(dest_dir: Path, owner: str, name: str, branch: str):
    """
    Download repository source-code from main

    Returns the download url.
    """
    url = f"https://github.com/{owner}/{name}.git"
    cmd = ("git", "clone", "--depth", "1", "--branch", branch, url, str(dest_dir))
    log.info("Downloading from Github with:\n{}".format(" ".join(cmd)))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log.error(
            f"An error ocurred while trying to download '{name}' source-code:\n{e}"
        )
        raise

    log.info("Done.")
    return url


def download_from_gh_latest(dest_dir: Path, owner: str, name: str):
    """
    Download repository source-code from latest GitHub Release (w/ GitHub API).

    See: https://docs.github.com/en/rest/releases/releases?apiVersion=2022-11-28#get-the-latest-release

    Returns the download url.
    """
    latest_release_link_url = (
        "https://api.github.com/repos/{}/{}/releases/latest".format(owner, name)
    )

    print("Fetching latest release with:", latest_release_link_url)
    response = httpx.get(latest_release_link_url)
    latest_release_tar_url = response.json()["tarball_url"]

    print("Downloadng tarball from:", latest_release_tar_url)
    response = httpx.get(latest_release_tar_url, follow_redirects=True)
    bytes_data = BytesIO(response.content)

    print("Extracting tarball to:", dest_dir)
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(fileobj=bytes_data) as tar:
            tar.extractall(tmpdir, filter="data")
        # workaround because I cant know the name of the extracted dir with tarfile lib
        dirname = Path(tmpdir) / tar.getmembers()[0].name.split()[0]
        shutil.move(str(dirname.absolute()), str(dest_dir.absolute()))
    # Reference:
    # https://www.python-httpx.org/quickstart/#binary-response-content
    # https://docs.python.org/3/library/tarfile.html#tarfile.TarFile.extractall
    return latest_release_tar_url


@dataclass
class Repos:
    """A collection of Repos"""

    core: Repo
    content: t.List[Repo] = field(default_factory=list)
    other: t.List[Repo] = field(default_factory=list)

    def update_local_checkouts(self):
        """Update repos to use local checkout, if exists in the parent dir of CWD"""
        for repo in self.all:
            checkout_dir = Path().absolute().parent / repo.name
            if repo.local_basepath is None and checkout_dir.exists():
                repo.status.use_local_checkout = True
                repo.local_basepath = Path().absolute().parent
                # looks like 'refs/head/main'
                checkout_refs = Path(checkout_dir / ".git" / "HEAD").read_text()
                checkout_refs = checkout_refs[len("ref: ") :].replace("\n", "")
                repo.status.original_refs = repo.branch
                repo.branch = checkout_refs

    @property
    def all(self):
        return [self.core] + self.content + self.other

    @classmethod
    def from_yaml(cls, path: str):
        """
        Load repositories listing from yaml file (repolist.yml)

        Example:
            ```yaml
            repos:
                core:
                  name:
                  title:
                content:
                  - name: pulp_rpm
                    title: Rpm Package
                  - name: pulp_maven
                    title: Maven
            ```
        """
        log.info("[pulp-docs] Loading repolist file from repofile.yml")
        file = Path(path)
        if not file.exists():
            raise ValueError("File does not exist:", file)
        log.info(f"repofile={str(file.absolute())}")

        with open(file, "r") as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)
        repos = data["repos"]
        core_repo = Repo(**repos["core"][0], type="core")
        content_repos = [Repo(**repo, type="content") for repo in repos["content"]]
        other_repos = [Repo(**repo, type="other") for repo in repos["other"]]
        return Repos(core=core_repo, content=content_repos, other=other_repos)

    @classmethod
    def test_fixtures(cls):
        """Factory of test Repos. Uses fixtures shipped in package data."""
        log.info("[pulp-docs] Loading repolist file from fixtures")
        DEFAULT_CORE = Repo("Pulp Core", "core", type="core")
        DEFAULT_CONTENT_REPOS = [
            Repo(
                "Rpm Packages",
                "new_repo1",
                local_basepath=FIXTURE_WORKDIR,
                type="content",
            ),
            Repo(
                "Debian Packages",
                "new_repo2",
                local_basepath=FIXTURE_WORKDIR,
                type="content",
            ),
            Repo("Maven", "new_repo3", local_basepath=FIXTURE_WORKDIR, type="content"),
        ]
        return Repos(core=DEFAULT_CORE, content=DEFAULT_CONTENT_REPOS)