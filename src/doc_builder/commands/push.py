# coding=utf-8
# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import base64
import logging
from pathlib import Path
from time import sleep, time
from typing import Dict, List, Optional

import requests
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport


def get_head_oid(repo_id: str, token: str, branch: Optional[str] = "main") -> str:
    """
    Returns last commit sha from repostory `repo_id` & branch `branch`
    """
    res = requests.get(
        f"https://api.github.com/repos/{repo_id}/git/refs/heads/{branch}",
        headers={"Authorization": f"bearer {token}"},
    )
    if res.status_code != 200:
        raise Exception(f"get_head_oid failed: {res.message}")

    res_json = res.json()
    head_oid = res_json["object"]["sha"]
    return head_oid


def create_additions(library_name: str) -> List[Dict]:
    """
    Given `library_name` dir, returns [FileAddition!]!: [{path: "some_path", contents: "base64_repr_contents"}, ...]
    see more here: https://docs.github.com/en/graphql/reference/input-objects#filechanges
    """
    p = Path(library_name)
    files = [x for x in p.glob("**/*") if x.is_file()]
    additions = []

    doc_version_folder = next(filter(lambda x: not x.is_file(), p.glob("*")), None).relative_to(p)
    doc_version_folder = str(doc_version_folder)

    for fpath in files:
        # do NOT commit _versions.yml on `main` branch builds as change in `_versions.yml` can collide with other doc builds
        if doc_version_folder == "main" and str(fpath).endswith("_versions.yml"):
            continue
        with open(fpath, "rb") as reader:
            content = reader.read()
            content_base64 = base64.b64encode(content)
            content_base64 = str(content_base64, "utf-8")
            additions.append({"path": str(fpath), "contents": content_base64})

    return additions


def create_deletions(
    repo_id: str, library_name: str, token: str, doc_version_folder: Optional[str] = None, is_delete_all: bool = False
) -> List[Dict]:
    """
    Given `repo_id/library_name` path, returns [FileDeletion!]!: [{path: "some_path"}, ...]
    see more here: https://docs.github.com/en/graphql/reference/input-objects#filechanges
    """
    # 1. find url for `doc-build-dev/{library_name}` ex: doc-build-dev/accelerate
    res = requests.get(
        f"https://api.github.com/repos/{repo_id}/git/trees/heads/main", headers={"Authorization": f"bearer {token}"}
    )
    if res.status_code != 200:
        raise Exception(f"create_deletions failed (GET tree root): {res.text}")
    json = res.json()
    node = next(filter(lambda node: node["path"] == library_name, json["tree"]), None)
    url = node["url"]

    # 2. find url for `doc-build-dev/{library_name}/{doc_version}` ex: doc-build-dev/accelerate/pr_365
    if doc_version_folder is None:
        root_folder = Path(library_name)
        doc_version_folder = next(filter(lambda x: not x.is_file(), root_folder.glob("*")), None).relative_to(
            root_folder
        )
        doc_version_folder = str(doc_version_folder)
    res = requests.get(url, headers={"Authorization": f"bearer {token}"})
    if res.status_code != 200:
        raise Exception(f"create_deletions failed (GET tree root/{repo_id}): {res.text}")
    json = res.json()
    node = next(filter(lambda node: node["path"] == doc_version_folder, json["tree"]), None)
    if node is None:
        # there is no need to delete since the path does not exist
        return []
    url = node["url"]

    # 3. list paths in `doc-build-dev/{library_name}/{doc_version}/**/*` ex: doc-build-dev/accelerate/pr_365/**/*
    res = requests.get(f"{url}?recursive=true", headers={"Authorization": f"bearer {token}"})
    if res.status_code != 200:
        raise Exception(f"create_deletions failed (GET tree root/{repo_id}/{doc_version_folder}): {res.text}")
    json = res.json()
    tree = json["tree"]

    if is_delete_all:
        # 4. deletios for all files found in current git tree
        deletions = [
            {"path": f"{library_name}/{doc_version_folder}/{node['path']}"} for node in tree if node["type"] == "blob"
        ]
    else:
        # only delete files that were not part of current doc build
        # 4. list paths in currently built doc folder
        built_docs_path = Path(f"{library_name}/{doc_version_folder}").absolute()
        built_docs_files = [x for x in built_docs_path.glob("**/*") if x.is_file()]
        built_docs_files_relative = set([str(f.relative_to(built_docs_path)) for f in built_docs_files])

        # 5. deletions = set difference between step 3 & 4
        deletions = [
            {"path": f"{library_name}/{doc_version_folder}/{node['path']}"}
            for node in tree
            if node["type"] == "blob" and node["path"] not in built_docs_files_relative
        ]

    return deletions


MAX_CHUNK_LEN = 3e7  # 30 Megabytes


def create_additions_chunks(additions: List[Dict]) -> List[List[Dict]]:
    """
    Github GraphQL `createCommitOnBranch` mutation fails when a payload is bigger than 50 MB.
    Therefore, in those cases (transformers doc ~ 100 MB), we need to commit using
    multiple smaller `createCommitOnBranch` mutation calls.
    """
    additions_chunks = []
    current_chunk = []
    current_len = 0

    for addition in additions:
        addition_len = len(addition["contents"])
        if current_len + addition_len < MAX_CHUNK_LEN:
            # can add to current_chunk
            current_chunk.append(addition)
            current_len += addition_len
        else:
            # create a new chunk
            additions_chunks.append(current_chunk)
            current_chunk = [addition]
            current_len = addition_len

    if current_chunk:
        additions_chunks.append(current_chunk)

    return additions_chunks


CREATE_COMMIT_ON_BRANCH_GRAPHQL = """
mutation (
  $repo_id: String!
  $additions: [FileAddition!]!
  $deletions: [FileDeletion!]!
  $head_oid: GitObjectID!
  $commit_msg: String!
) {
  createCommitOnBranch(
    input: {
      branch: { repositoryNameWithOwner: $repo_id, branchName: "main" }
      message: { headline: $commit_msg }
      fileChanges: { additions: $additions, deletions: $deletions }
      expectedHeadOid: $head_oid
    }
  ) {
    clientMutationId
  }
}
"""


def create_commit(
    client: Client, repo_id: str, additions: List[Dict], deletions: List[Dict], token: str, commit_msg: str
):
    """
    Commits additions and/or deletions to a repository using Github GraphQL mutation `createCommitOnBranch`
    see more here: https://docs.github.com/en/graphql/reference/mutations#createcommitonbranch
    """
    # Provide a GraphQL query
    query = gql(CREATE_COMMIT_ON_BRANCH_GRAPHQL)
    head_oid = get_head_oid(repo_id, token)
    params = {
        "additions": additions,
        "deletions": deletions,
        "repo_id": repo_id,
        "head_oid": head_oid,
        "commit_msg": commit_msg,
    }
    # Execute the query
    result = client.execute(query, variable_values=params)
    return result


def push_command(args):
    """
    Commit file additions and/or deletions using Github GraphQL rather than `git`.
    Usage: doc-builder push $args
    """
    if args.n_retries < 1:
        raise ValueError(f"CLI arg `n_retries` MUST be positive & non-zero; supplied value was {args.n_retries}")
    if args.is_remove:
        push_command_remove(args)
    else:
        push_command_add(args)


def push_command_add(args):
    """
    Commit file changes (additions & deletions) using Github GraphQL rather than `git`.
    Used in: build_main_documentation.yml & build_pr_documentation.yml
    """
    max_n_retries = args.n_retries + 1
    number_of_retries = args.n_retries
    n_seconds_sleep = 5
    # file deletions
    deletions = create_deletions(args.doc_build_repo_id, args.library_name, args.token)
    # file additions
    time_start = time()
    additions = create_additions(args.library_name)
    time_end = time()
    logging.debug(f"create_additions took {time_end-time_start:.4f} seconds or {(time_end-time_start)/60.0:.2f} mins")
    additions_chunks = create_additions_chunks(additions)

    time_start = time()
    while number_of_retries:
        try:
            # Create Github GraphQL client
            transport = RequestsHTTPTransport(
                url="https://api.github.com/graphql", headers={"Authorization": f"bearer {args.token}"}, verify=True
            )
            with Client(transport=transport, fetch_schema_from_transport=True, execute_timeout=None) as gql_client:
                # commit file deletions
                create_commit(
                    gql_client, args.doc_build_repo_id, [], deletions, args.token, f"Deleteions: {args.commit_msg}"
                )
                # commit push chunks additions
                for i, additions in enumerate(additions_chunks):
                    create_commit(gql_client, args.doc_build_repo_id, additions, [], args.token, args.commit_msg)
                    print(f"Committed additions chunk: {i+1}/{len(additions_chunks)}")
            break
        except Exception as e:
            number_of_retries -= 1
            print(f"createCommitOnBranch error occurred: {e}")
            if number_of_retries:
                print(f"Failed on try #{max_n_retries-number_of_retries}, pushing again in {n_seconds_sleep} seconds")
                sleep(n_seconds_sleep)
            else:
                raise RuntimeError("create_commit additions failed") from e

    time_end = time()
    logging.debug(f"commit_additions took {time_end-time_start:.4f} seconds or {(time_end-time_start)/60.0:.2f} mins")


def push_command_remove(args):
    """
    Commit file deletions only using Github GraphQL rather than `git`.
    Used in: delete_doc_comment.yml
    """
    max_n_retries = args.n_retries + 1
    number_of_retries = args.n_retries
    n_seconds_sleep = 5
    doc_version_folder = args.doc_version
    # file deletions
    deletions = create_deletions(args.doc_build_repo_id, args.library_name, args.token, doc_version_folder, True)

    while number_of_retries:
        try:
            # Create Github GraphQL client
            transport = RequestsHTTPTransport(
                url="https://api.github.com/graphql", headers={"Authorization": f"bearer {args.token}"}, verify=True
            )
            with Client(transport=transport, fetch_schema_from_transport=True, execute_timeout=None) as gql_client:
                # commit file deletions
                create_commit(gql_client, args.doc_build_repo_id, [], deletions, args.token, args.commit_msg)
            break
        except Exception as e:
            number_of_retries -= 1
            print(f"createCommitOnBranch error occurred: {e}")
            if number_of_retries:
                print(f"Failed on try #{max_n_retries-number_of_retries}, pushing again in {n_seconds_sleep} seconds")
                sleep(n_seconds_sleep)
            else:
                raise RuntimeError("create_commit additions failed") from e


def push_command_parser(subparsers=None):
    if subparsers is not None:
        parser = subparsers.add_parser("push")
    else:
        parser = argparse.ArgumentParser("Doc Builder push command")

    parser.add_argument(
        "library_name",
        type=str,
        help="The name of the library, which also acts as a path where built doc artifacts reside in",
    )
    parser.add_argument(
        "--doc_build_repo_id",
        type=str,
        help="Repo to which doc artifcats will be committed (e.g. `huggingface/doc-build-dev`)",
    )
    parser.add_argument("--token", type=str, help="Github token that has write/push premission to `doc_build_repo_id`")
    parser.add_argument(
        "--commit_msg",
        type=str,
        help="Git commit message",
        default="Github GraphQL createcommitonbranch commit",
    )
    parser.add_argument("--n_retries", type=int, help="Number of push retries in the event of conflict", default=1)
    parser.add_argument(
        "--doc_version",
        type=str,
        default=None,
        help="Version of the generated documentation.",
    )
    parser.add_argument(
        "--is_remove",
        action="store_true",
        help="Whether or not to remove entire folder ('--doc_version') from git tree",
    )

    if subparsers is not None:
        parser.set_defaults(func=push_command)
    return parser
