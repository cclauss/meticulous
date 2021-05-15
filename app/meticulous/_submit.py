"""
Main processing for meticulous
"""
from __future__ import absolute_import, division, print_function

import io
import os
import pathlib
import re
import sys
from pathlib import Path

from github import GithubException
from plumbum import FG, ProcessExecutionError, local

from meticulous._exceptions import ProcessingFailed
from meticulous._github import create_pr, get_api, get_parent_repo
from meticulous._input import UserCancel, make_choice, make_simple_choice
from meticulous._processrepo import add_repo_save
from meticulous._storage import get_json_value, set_json_value
from meticulous._summary import display_and_check_files
from meticulous._util import get_editor

MULTI_SAVE_KEY = "repository_saves_multi"
ALWAYS_BATCH_MODE = True
ALWAYS_ISSUE_AND_BRANCH = True
ALWAYS_PLAIN_PR = True


def get_note(kind, pr_url=None):
    """
    Obtain semi-automation warning
    """
    header = (
        f"Semi-automated {kind} generated by\n"
        f"https://github.com/timgates42/meticulous"
        f"/blob/master/docs/NOTE.md"
    )
    if pr_url is None:
        return header
    api = get_api()
    user_org = api.get_user().login
    return f"""\
{header}

To avoid wasting CI processing resources a branch with the fix has been
prepared but a pull request has not yet been created. A pull request fixing
the issue can be prepared from the link below, feel free to create it or
request @{user_org} create the PR.

{pr_url}

Thanks.
"""


def submit_handlers():
    """
    Obtain multithread task handlers for submission.
    """
    return {
        "submit": submit,
        "issue_and_branch": issue_and_branch,
        "plain_pr": plain_pr,
        "full_pr": full_pr,
    }


def submit(context):
    """
    Task to submit a pull request/issue
    repository is clean
    """

    def handler():
        reponame = context.taskjson["reponame"]
        orig_repository_saves_multi = get_json_value(MULTI_SAVE_KEY, [])
        repository_saves_multi = [
            reposave
            for reposave in orig_repository_saves_multi
            if reposave["reponame"] == reponame
        ]
        if len(repository_saves_multi) == 1:
            reposave = repository_saves_multi[0]
            if not ALWAYS_PLAIN_PR:
                suggest_plain = check_if_plain_pr(reposave)
                add_word = reposave["add_word"]
                del_word = reposave["del_word"]
                file_paths = reposave["file_paths"]
                files = ", ".join(file_paths)
                context.interaction.send(
                    f"Fix in {reponame}: {del_word} -> {add_word} over {files}"
                )
                if suggest_plain:
                    submit_plain = context.interaction.get_confirmation(
                        "Analysis suggests plain pr, agree?"
                    )
                else:
                    submit_plain = context.interaction.get_confirmation(
                        "Complex repo submit plain pr anyway?"
                    )
            else:
                submit_plain = True
        else:
            submit_plain = False
        context.controller.add(
            {
                "name": "issue_and_branch"
                if ALWAYS_ISSUE_AND_BRANCH
                else ("plain_pr" if submit_plain else "full_pr"),
                "interactive": False,
                "reponame": reponame,
                "repository_saves_multi": repository_saves_multi,
            }
        )
        set_json_value(MULTI_SAVE_KEY, [])

    return handler


def plain_pr(context):
    """
    Non-interactive task to finish off submission of a pr
    """

    def handler():
        reponame = context.taskjson["reponame"]
        repository_saves_multi = context.taskjson["repository_saves_multi"]
        plain_pr_for(reponame, repository_saves_multi)
        add_cleanup(context, reponame)

    return handler


def full_pr(context):
    """
    Non-interactive task to finish off submission of a pr
    """

    def handler():
        reponame = context.taskjson["reponame"]
        repository_saves_multi = context.taskjson["repository_saves_multi"]
        full_pr_for(reponame, repository_saves_multi)
        add_cleanup(context, reponame)

    return handler


def issue_and_branch(context):
    """
    Non-interactive task to finish off submission of a pr
    """

    def handler():
        reponame = context.taskjson["reponame"]
        repository_saves_multi = context.taskjson["repository_saves_multi"]
        issue_and_branch_for(reponame, repository_saves_multi)
        add_cleanup(context, reponame)

    return handler


def add_cleanup(context, reponame):
    """
    Kick off cleanup on completion
    """
    context.controller.add(
        {"name": "cleanup", "interactive": True, "priority": 20, "reponame": reponame}
    )


def fast_prepare_a_pr_or_issue_for(reponame, reposave):
    """
    Display a suggestion if the repository looks like it wants an issue and a
    pull request or is happy with just a pull request.
    """
    if check_if_plain_pr(reposave):
        plain_pr_for(reponame, [reposave])
    else:
        prepare_a_pr_or_issue_for(reponame, reposave)


def check_if_plain_pr(reposave):
    """
    Display a suggestion if the repository looks like it wants an issue and a
    pull request or is happy with just a pull request.
    """

    repopath = Path(reposave["repodir"])
    suggest_issue = False
    if display_and_check_files(repopath / ".github" / "ISSUE_TEMPLATE"):
        suggest_issue = True
    if display_and_check_files(repopath / ".github" / "pull_request_template.md"):
        suggest_issue = True
    if display_and_check_files(repopath / "CONTRIBUTING.md"):
        suggest_issue = True
    if not suggest_issue:
        return True
    return False


def plain_pr_for(reponame, repository_saves_multi):
    """
    Create and submit the standard PR.
    """
    make_a_commit_multi(reponame, repository_saves_multi, False)
    non_interactive_submit_commit_multi(reponame, repository_saves_multi)


def full_pr_for(reponame, repository_saves_multi):
    """
    Create and submit the standard PR.
    """
    make_issue_multi(reponame, repository_saves_multi, True)
    submit_issue_multi(reponame, repository_saves_multi, None)
    non_interactive_submit_commit_multi(reponame, repository_saves_multi)


def issue_and_branch_for(reponame, repository_saves_multi):
    """
    Create an issue and a branch that is ready to create a PR but has not yet
    created the PR this can avoid wasting CI processing if the issue will never
    be accepted and is still quite convenient.
    """
    no_issues = Path("__no_issues__.txt")
    repodir = reposave["repodir"]
    repodirpath = Path(repodir)
    no_issues_path = repodirpath / no_issues
    if no_issues_path.is_file():
        plain_pr_for(reponame, repository_saves_multi)
        return
    make_a_commit_multi(reponame, repository_saves_multi, False)
    _, _, from_branch, to_branch = non_interactive_prepare_commit_multi(
        repository_saves_multi
    )
    api = get_api()
    user_org = api.get_user().login
    pr_url = f"https://github.com/{user_org}/{reponame}/pull/new/{from_branch}"
    make_issue_multi(reponame, repository_saves_multi, True, pr_url=pr_url)
    submit_issue_multi(reponame, reposave, None)
    amend_commit(reposave, from_branch, to_branch)


def prepare_a_pr_or_issue_for(reponame, reposave):
    """
    Access repository to prepare a change
    """
    try:
        while True:
            repodir = reposave["repodir"]
            repodirpath = Path(repodir)
            choices = get_pr_or_issue_choices(reponame, repodirpath)
            option = make_choice(choices)
            if option is None:
                return
            handler, context = option
            handler(reponame, reposave, context)
    except UserCancel:
        print("quit - returning to main process")


def get_pr_or_issue_choices(reponame, repodirpath):  # pylint: disable=too-many-locals
    """
    Work out the choices menu for pr/issue
    """
    issue_template = Path(".github") / "ISSUE_TEMPLATE"
    pr_template = Path(".github") / "pull_request_template.md"
    contrib_guide = Path("CONTRIBUTING.md")
    issue = Path("__issue__.txt")
    commit = Path("__commit__.txt")
    prpath = Path("__pr__.txt")
    no_issues = Path("__no_issues__.txt")
    choices = {}
    paths = (
        issue_template,
        pr_template,
        contrib_guide,
        prpath,
        issue,
        commit,
        no_issues,
    )
    for path in paths:
        has_path = (repodirpath / path).exists()
        print(f"{reponame} {'HAS' if has_path else 'does not have'}" f" {path}")
        if has_path:
            choices[f"show {path}"] = (show_path, path)
    choices["make a commit"] = (make_a_commit, False)
    choices["make a full issue"] = (make_issue, True)
    choices["make a short issue"] = (make_issue, False)
    has_issue = (repodirpath / issue).exists()
    if has_issue:
        choices["submit issue"] = (submit_issue, None)
    has_commit = (repodirpath / commit).exists()
    if has_commit:
        choices["submit commit"] = (submit_commit, None)
        choices["submit issue"] = (submit_issue, None)
    return choices


def make_issue(
    reponame, reposave, is_full, pr_url=None
):  # pylint: disable=unused-argument
    """
    Prepare an issue template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    title = f"Fix simple typo: {del_word} -> {add_word}"
    if is_full:
        body = f"""\
# Issue Type

[x] Bug (Typo)

# Steps to Replicate

1. Examine {files}.
2. Search for `{del_word}`.

# Expected Behaviour

1. Should read `{add_word}`.

{get_note('issue', pr_url)}
"""
    else:
        body = f"""\
There is a small typo in {files}.
Should read `{add_word}` rather than `{del_word}`.

{get_note('issue', pr_url)}
"""
    with io.open(str(repodir / "__issue__.txt"), "w", encoding="utf-8") as fobj:
        print(title, file=fobj)
        print("", file=fobj)
        print(body, file=fobj)


def make_a_commit(reponame, reposave, is_full):  # pylint: disable=unused-argument
    """
    Prepare a commit template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
docs: fix simple typo, {del_word} -> {add_word}

There is a small typo in {files}.

Should read `{add_word}` rather than `{del_word}`.
""",
            file=fobj,
        )


def make_a_commit_multi(
    reponame, reposaves, is_full
):  # pylint: disable=unused-argument
    """
    Prepare a commit template file
    """
    if not reposaves:
        return
    if len(reposaves) == 1:
        make_a_commit(reponame, reposaves[0], is_full)
        return
    file_paths = list(set(sum((reposave["file_paths"] for reposave in reposaves), [])))
    file_paths.sorted()
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = "\n".join([f"- {file_path}" for file_path in file_paths])
    lines = "\n".join(
        [
            f"- Should read `{repo_save['add_word']}`"
            f" rather than `{repo_save['del_word']}`."
            for reposave in reposaves
        ]
    )
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
docs: Fix a few typos

There are small typos in:
{files}

Fixes:
{lines}
""",
            file=fobj,
        )


def submit_issue(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up an issue
    """
    repodir = Path(reposave["repodir"])
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    files = ", ".join(file_paths)
    issue_path = str(repodir / "__issue__.txt")
    title, body = load_commit_like_file(issue_path)
    issue_num = issue_via_api(reponame, title, body)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
docs: fix simple typo, {del_word} -> {add_word}

There is a small typo in {files}.

Closes #{issue_num}
""",
            file=fobj,
        )


def issue_via_api(reponame, title, body):
    """
    Create an issue via the API
    """
    repo = get_parent_repo(reponame)
    issue = repo.create_issue(title=title, body=body)
    return issue.number


def load_commit_like_file(path):
    """
    Read title and body from a well formatted git commit
    """
    with io.open(path, "r", encoding="utf-8") as fobj:
        title = fobj.readline().strip()
        blankline = fobj.readline().strip()
        if blankline != "":
            raise Exception(f"Needs to be a blank second line for {path}.")
        body = fobj.read()
    return title, body


def submit_commit(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up a commit and show message
    """
    print(non_interactive_submit_commit_multi(reponame, [reposave]))


def non_interactive_submit_commit_multi(reponame, reposaves):
    """
    Push up a PR from a commit
    """
    try:
        title, body, from_branch, to_branch = (
            non_interactive_prepare_commit_multi(reposaves)
        )
        body += f"\n{get_note('pull request')}"
        pullreq = create_pr(reponame, title, body, from_branch, to_branch)
        return f"Created PR #{pullreq.number} view at {pullreq.html_url}"
    except ValueError:
        return f"Failed to process {reponame}."
    except ProcessExecutionError:
        return f"Failed to commit for {reponame}."
    except GithubException:
        return f"Failed to create pr for {reponame}."


def non_interactive_prepare_commit_multi(reposaves):
    """
    Push up a commit
    """
    if not reposaves:
        raise ValueError("No fixes to prepare")
    reposave = reposaves[0]
    if any(reposave["repodir"] != check["repodir"] for check in reposaves):
        raise ValueError("Mismatch in repositories preparing commit")
    repodir = Path(reposave["repodir"])
    commit_path = str(repodir / "__commit__.txt")
    title, body = load_commit_like_file(commit_path)
    if len(reposaves) == 1:
        add_word = reposave["add_word"]
        branch_name = f"bugfix_typo_{add_word.replace(' ', '_')}"
    else:
        branch_name = "bugfix_typos"
    to_branch = push_commit_multi(repodir, branch_name)
    return title, body, branch_name, to_branch


def push_commit_multi(repodir, branch_name):
    """
    Create commit and push
    """
    git = local["git"]
    # plumbum bug workaround
    os.chdir(pathlib.Path.home())
    with local.cwd(repodir):
        to_branch = git("symbolic-ref", "--short", "HEAD").strip()
        git("commit", "-F", "__commit__.txt")
        git("push", "origin", f"{to_branch}:{branch_name}")
    return to_branch


def amend_commit(reposave, from_branch, to_branch):
    """
    Update commit message to include issue number
    """
    repodir = reposave["repodir"]
    git = local["git"]
    # plumbum bug workaround
    os.chdir(pathlib.Path.home())
    with local.cwd(repodir):
        git("commit", "-F", "__commit__.txt", "--amend")
        git("push", "origin", "-f", f"{to_branch}:{from_branch}")


def show_path(reponame, reposave, path):  # pylint: disable=unused-argument
    """
    Display the issue template directory
    """
    print("Opening editor")
    editor = local[get_editor()]
    repodir = reposave["repodir"]
    # plumbum bug workaround
    os.chdir(pathlib.Path.home())
    with local.cwd(repodir):
        _ = editor[str(path)] & FG


def add_change_for_repo(repodir):
    """
    Work out the staged commit and prepare an issue and pull request based on
    the change
    """
    del_word, add_word, file_paths = get_typo(repodir)
    print(f"Changing {del_word} to {add_word} in {', '.join(file_paths)}")
    option = make_simple_choice(["save"], "Do you want to save?")
    if option == "save":
        add_repo_save(repodir, add_word, del_word, file_paths)


def get_typo(repodir):
    """
    Look in the staged commit for the typo.
    """
    git = local["git"]
    del_lines = []
    add_lines = []
    file_paths = []
    # plumbum bug workaround
    os.chdir(pathlib.Path.home())
    with local.cwd(repodir):
        output = git("diff", "--staged")
        for line in output.splitlines():
            if line.startswith("--- a/"):
                index = len("--- a/")
                file_path = line[index:]
                file_paths.append(file_path)
        for line in output.splitlines():
            if line.startswith("-") and not line.startswith("--- "):
                del_lines.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++ "):
                add_lines.append(line[1:])
    if not del_lines or not add_lines:
        print("Could not read diff", file=sys.stderr)
        raise ProcessingFailed()
    del_words = re.findall("[a-zA-Z]+", del_lines[0])
    add_words = re.findall("[a-zA-Z]+", add_lines[0])
    for del_word, add_word in zip(del_words, add_words):
        if del_word != add_word:
            return del_word, add_word, file_paths
    print("Could not locate typo", file=sys.stderr)
    raise ProcessingFailed()
