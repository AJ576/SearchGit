"""Experimental script exploring Git object inspection with Dulwich."""

from dulwich.repo import Repo


def tree_reader(path: str):
    """Walks all commits in a Git repository and counts unique trees and blobs.

    This function opens a repository, iterates through its commit history using
    Dulwich's walker, extracts the root tree for each commit, and triggers a
    recursive traversal (via `tree_walker`) for every root tree not yet seen.

    Args:
        path (str): File system path to the Git repository (e.g., 'physics_engine.git').
    """
    repo = Repo(path)

    commit_count = 0
    seen_tree = set()
    seen_blob = set()

    for entry in repo.get_walker():
        # get the commit sha of every id
        commit = entry.commit
        commit_count += 1
        # find the root tree in that commit (ONLY 1 root tree per commit)
        tree_sha = commit.tree
        # add to seen tree if not visited before
        if tree_sha not in seen_tree:
            seen_tree.add(tree_sha)
            # recurse down to get subtrees and all blob objects inside it
            tree = repo[tree_sha]
            tree_walker(repo, tree, seen_tree, seen_blob)
    # print out the summary counts
    print("tree_counts: ", len(seen_tree))
    print("blob_counts: ", len(seen_blob))
    print("commits: ", commit_count)


def tree_walker(repo, tree, seen_tree, seen_blob):
    """Recursively traverses entries in a Git tree object.

    Inspects each entry in the provided tree:
      - If the entry mode is 16384 (0o040000, directory/subtree), records its SHA
        in `seen_tree` and recurses into the subtree.
      - Otherwise, treats the entry as a blob (file) and records its SHA in `seen_blob`.

    Args:
        repo (Repo): An open Dulwich repository instance used to fetch object data.
        tree (Tree): The Git tree object to iterate through.
        seen_tree (set): Set of tree SHAs already visited (modified in-place).
        seen_blob (set): Set of blob SHAs already visited (modified in-place).
    """
    # loop thru all items in the tree
    for name, node, obj_sha in tree.iteritems():
        # if the node is a tree node (directory, mode 16384 == 0o040000) then recurse
        if node == 16384:
            print('TREE: ', name)
            # obj_sha points to another tree
            if obj_sha not in seen_tree:
                seen_tree.add(obj_sha)
                # get the actual tree object and recurse on it
                obj = repo[obj_sha]
                tree_walker(repo, obj, seen_tree, seen_blob)
        # else it's a blob (file)
        else:
            if obj_sha not in seen_blob:
                seen_blob.add(obj_sha)
            print('BLOB: ', name)
# def tree_walker(path:str):
#     repo = Repo(path)
#     head_obj = repo[repo.head()]
#     #start of the tree pointer or something i guess
#     tree = repo[head_obj.tree]

#     for name, node, sha in tree.iteritems():
#         print(name, node, sha)

tree_reader("physics_engine.git")


# for commit in repo.iter_commits():
#     print(commit.hexsha)
#     print(commit.message)

# commit = next(repo.iter_commits())

# tree = commit.tree

# for item in tree.traverse():
#     print(item.path, item.type)
# for item in commit.tree.traverse():
#     if item.type == "blob":
#         content = item.data_stream.read()
#         print(item.path)
#         print(content.decode("utf-8", errors="ignore"))