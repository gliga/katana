import logging
import re
from os import environ
from pathlib import Path
from typing import Optional, Tuple

from packaging import version
from packaging.version import Version

from . import CONFIG_VERSION_PATH, Configuration, git, SUBMODULE_PATH
from .commands import capture_command, CommandError

__all__ = ["get_version", "format_version_pep440", "format_version_debian"]

logger = logging.getLogger(__name__)

VERSION_TAG_RE = re.compile(r"v(?P<version>[^a-zA-Z][.0-9+a-zA-Z]+)")


def get_version(
    config: Configuration = None, commit=None, variant=None, *, pretend_master=False, pretend_clean=False
) -> Version:
    """
    Get the version of the code in the repositories specified in config. This will always return a version (even if
    it's incomplete) for the working copy (`commit` = None). It may throw an exception if the version of a specific
    commit is requested.
    :param config: A description of the repositories to analyze.
    :param commit: The commit to get the version of, or None to get the version of the working copy.
    :param variant: The build variant. This may only be used if the source code does not specify a variant.
    :return: The version of the Katana package.
    """
    config = config or Configuration()
    katana_repo_path = config.katana_repo_path
    katana_enterprise_repo_path = config.katana_enterprise_repo_path

    # Find commits in both repos.
    if commit is None:
        use_working_copy = True
        k_commit = None
        ke_commit = None
    else:
        use_working_copy = False
        here = git.get_working_tree(Path.cwd())
        if here == katana_repo_path:
            k_commit = git.get_hash(commit, katana_repo_path, pretend_clean=True)
            if config.has_enterprise:
                logger.warning(
                    f"Computing historic versions based on an {SUBMODULE_PATH} commit is limited. Producing "
                    "open-source build version."
                )
                katana_enterprise_repo_path = None
                config.katana_enterprise_repo_path = None  # Make has_enterprise = False
            ke_commit = None
        elif here == katana_enterprise_repo_path:
            ke_commit = commit
            k_commit = git.submodule_commit_at(SUBMODULE_PATH, ke_commit, katana_enterprise_repo_path)
        else:
            raise ValueError(
                "To specify a commit you must be in either katana or katana-enterprise to tell me which repo you want "
                "to use to find the commit."
            )

    k_commit = k_commit or "HEAD"
    ke_commit = ke_commit or "HEAD"

    if config.has_git:
        k_commit = git.simplify_merge_commit(k_commit, katana_repo_path)
        if config.has_enterprise:
            ke_commit = git.simplify_merge_commit(ke_commit, katana_enterprise_repo_path)

    k_explicit_version, variant = get_explicit_version(config, k_commit, use_working_copy, katana_repo_path, variant)
    ke_tag_version = None
    if config.has_enterprise and not git.is_dirty(katana_enterprise_repo_path):
        ke_tag_version = get_tag_version(config, ke_commit or "HEAD", katana_enterprise_repo_path)

    if k_explicit_version.is_devrelease or (not ke_tag_version and config.has_enterprise):
        explicit_version = add_dev_to_version(k_explicit_version)
    else:
        explicit_version = k_explicit_version

    if pretend_master or not use_working_copy:
        pretend_clean = True

    if pretend_master:
        core_branch = f"{config.upstream_remote}/master"
        enterprise_core_branch = f"{config.upstream_remote}/master"
        is_merged = True
    else:
        is_enterprise_merged = True
        enterprise_core_branch = None
        if config.has_enterprise:
            enterprise_core_branch = git_find_closest_core_branch(config, ke_commit, katana_enterprise_repo_path)
            is_enterprise_merged = enterprise_core_branch and git.is_ancestor_of(
                ke_commit, enterprise_core_branch, dir=katana_enterprise_repo_path
            )
        core_branch = git_find_closest_core_branch(config, k_commit, katana_repo_path) or enterprise_core_branch
        is_merged = core_branch and git.is_ancestor_of(k_commit, core_branch, dir=katana_repo_path)
        is_merged = is_enterprise_merged and is_merged

    k_count = None
    k_hash = None
    ke_count = None
    ke_hash = None
    if config.has_git:
        k_last_version_commit = git.find_change(katana_repo_path / CONFIG_VERSION_PATH, k_commit, katana_repo_path)
        k_count = compute_commit_count(
            config, k_commit, k_last_version_commit, katana_repo_path, pretend_master, core_branch
        )
        k_hash = git.get_hash(k_commit, katana_repo_path, pretend_clean=pretend_clean, abbrev=6)

        if config.has_enterprise:
            ke_last_version_commit = git_find_super_commit(
                k_last_version_commit, ke_commit, katana_enterprise_repo_path, katana_repo_path
            )
            ke_count = (
                compute_commit_count(
                    config,
                    ke_commit,
                    ke_last_version_commit,
                    katana_enterprise_repo_path,
                    pretend_master,
                    enterprise_core_branch,
                )
                if ke_last_version_commit
                else "xxx"
            )
            ke_hash = git.get_hash(
                ke_commit,
                katana_enterprise_repo_path,
                pretend_clean=pretend_clean,
                exclude_dirty=(SUBMODULE_PATH,),
                abbrev=6,
            )

    computed_version = katana_version(
        *explicit_version.release,
        k_count,
        ke_count,
        k_hash,
        ke_hash,
        variant=variant,
        dev=explicit_version.is_devrelease,
        pre=explicit_version.pre,
        post=explicit_version.post,
        is_merged=is_merged,
    )
    if config.version_from_environment_variable:
        env_version = config.version_from_environment_variable
        if env_version.release != computed_version.release:
            logger.warning(
                "The KATANA_VERSION environment variable does not match the version in the source code: "
                f"{env_version} does not match {computed_version}"
            )
        return env_version
    else:
        return computed_version


def git_find_closest_core_branch(config, commit, repo_path):
    if not config.has_git:
        return None

    branch_patterns = [
        f"{config.upstream_remote}/master",
        f"{config.upstream_remote}/release/v*",
        f"{config.upstream_remote}/variant/*",
    ]
    branches = [
        b for pat in branch_patterns for b in git.find_branches(pat, repo_path, prefix="remotes", sort="-creatordate")
    ]

    if not branches:
        return None

    def branch_ahead_count(branch):
        return git.get_commit_count(git.merge_base(commit, branch, repo_path), commit, repo_path)

    nearest_branch = min(branches, key=branch_ahead_count)
    return nearest_branch


def get_explicit_version(config, k_commit: str, use_working_copy: bool, katana_repo_path, variant=None, no_dev=False):
    tag_version = get_tag_version(config, k_commit, katana_repo_path)
    explicit_version = tag_version or get_config_version(
        None if use_working_copy else k_commit, katana_repo_path, no_dev=no_dev
    )
    if explicit_version.local and variant and variant != explicit_version.local:
        logger.warning(
            f"You are overriding the repository variant {explicit_version.local} with build-time variant {variant}."
        )
    variant = variant or explicit_version.local
    return explicit_version, variant


def get_config_version(k_commit, katana_repo_path, no_dev=False) -> version.Version:
    if k_commit:
        version_str = capture_command(
            "git", *git.dir_arg(katana_repo_path), "show", "-p", f"{k_commit}:{CONFIG_VERSION_PATH}"
        )
    else:
        with open(katana_repo_path / CONFIG_VERSION_PATH, "rt") as version_file:
            version_str = version_file.read()
    ver = version.Version(version_str.strip())

    if no_dev:
        return ver

    return add_dev_to_version(ver)


def get_tag_version(config, commit, repo_path):
    if not config.has_git or not commit:
        return None
    tag_version = None
    version_tags = [m for m in (VERSION_TAG_RE.match(t) for t in git.get_tags_of(commit, repo_path)) if m]
    if len(version_tags) > 1:
        logger.warning(f"There is more than one version tag at the given commit. Picking one arbitrarily.")
    if version_tags:
        tag_version = version.Version(version_tags[0].group("version"))
    return tag_version


def compute_commit_count(config, commit, last_version_commit, repo_path, pretend_master, core_branch):
    if not pretend_master:
        if not core_branch:
            logger.warning(
                f"Cannot determine the commit count at {commit} (replacing with 'x'). Make sure you have git history "
                f"on master, release, and variant branches back to the last change to 'config/version.txt' to avoid "
                f"this issue."
            )
            return "x"
        last_core_commit = git.merge_base(commit, core_branch, repo_path)
    else:
        last_core_commit = commit
    return git.get_commit_count(last_version_commit, last_core_commit, repo_path)


def git_find_super_commit(submodule_commit_to_find, super_commit, super_repo_path, sub_repo_path):
    """
    Find the super module commit which introduced the provided submodule commit.

    :return: The super commit hash.
    """
    submodule_changes = git.find_changes(sub_repo_path, super_commit, super_repo_path, n=None)

    for i, commit in enumerate(submodule_changes):
        submodule_commit = git.submodule_commit_at(SUBMODULE_PATH, commit, dir=super_repo_path)
        try:
            if git.is_ancestor_of(submodule_commit_to_find, submodule_commit, dir=sub_repo_path):
                continue
        except CommandError as e:
            if "is a tree" in str(e):
                logger.warning(
                    f"Reached repository restructure commit ({submodule_commit}). Picking that commit as version "
                    f"change commit. This is weird, but provides semi-useful versions for commits before the "
                    f"versioning system was introduced fully."
                )
                return commit
            logger.info(f"Encountered bad commit in {super_repo_path.name}. Skipping. Error: {e}")
            continue
        # submodule_commit_to_find is not an ancestor of commit
        return submodule_changes[i - 1]

    return None


def add_dev_to_version(ver):
    # Now a terrible hack to add .dev, No there is no way to just set it or create a version without a string.
    parts = []
    # Epoch
    if ver.epoch != 0:
        parts.append("{0}:".format(ver.epoch))
    # Release segment
    parts.append(".".join(str(x) for x in ver.release))
    # Pre-release
    if ver.pre is not None:
        parts.append("".join(str(x) for x in ver.pre))
    # Post-release
    if ver.post is not None:
        parts.append(".post{0}".format(ver.post))
    parts.append(".dev0")
    # Local version segment
    if ver.local is not None:
        parts.append("+{0}".format(ver.local))
    return version.Version("".join(parts))


def katana_version(
    major: int,
    minor: int,
    micro: int,
    k_count: int,
    ke_count: Optional[int],
    k_hash,
    ke_hash,
    *,
    variant: Optional[str] = None,
    dev: bool = False,
    pre: Optional[Tuple[str, int]] = None,
    post: Optional[int] = None,
    is_merged: bool,
):
    s = f"{major}.{minor}.{micro}"
    if pre is not None:
        s += f"{pre[0]}{pre[1]}"
    if post is not None:
        assert isinstance(post, int), post
        s += f".post{post}"
    assert isinstance(dev, bool), dev
    if dev:
        s += ".dev"
    k_count = k_count if k_count is not None else "x"
    k_hash = k_hash or "xxxxxx"
    if ke_count is not None and ke_hash:
        dev_tag = f"{k_count}.{ke_count}.{k_hash}.{ke_hash}"
    else:
        dev_tag = f"{k_count}.0.{k_hash}"
    if not is_merged:
        dev_tag += ".unmerged"
    if variant is not None or dev:
        s += "+"
        if variant is not None:
            assert len(variant) > 0
            s += f"{variant}"
        if dev:
            if variant is not None:
                s += "."
            s += dev_tag
    v = version.Version(s)
    return v


def format_version_pep440(ver: version.Version) -> str:
    parts = []

    # Epoch
    if ver.epoch != 0:
        parts.append("{0}:".format(ver.epoch))

    # Release segment
    parts.append(".".join(str(x) for x in ver.release))

    # Pre-release
    if ver.pre is not None:
        parts.append("".join(str(x) for x in ver.pre))

    # Post-release
    if ver.post is not None:
        parts.append(".post{0}".format(ver.post))

    # Development release
    if ver.dev is not None:
        parts.append(".dev{0}".format(ver.dev or ""))

    # Local version segment
    if ver.local is not None:
        parts.append("+{0}".format(ver.local))

    return "".join(parts)


def format_version_debian(ver: version.Version) -> str:
    parts = []

    # Epoch
    if ver.epoch != 0:
        parts.append("{0}:".format(ver.epoch))

    # Release segment
    parts.append(".".join(str(x) for x in ver.release))

    # Pre-release
    if ver.pre is not None:
        parts.append("~" + "".join(str(x) for x in ver.pre))

    # Post-release
    if ver.post is not None:
        parts.append(".post{0}".format(ver.post))

    # Development release
    if ver.dev is not None:
        parts.append("~dev{0}".format(ver.dev or ""))

    # Local version segment
    if ver.local is not None:
        parts.append("+{0}".format(ver.local))

    return "".join(parts)