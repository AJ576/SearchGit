from dulwich.repo import Repo


def tree_reader(path:str):
    repo = Repo(path)

    commit_count = 0
    seen_tree = set()
    seen_blob = set()


    for entry in repo.get_walker():
        #get the commit sha of ever id?
        commit = entry.commit
        commit_count+=1
        #find the root tree in that commit(ONLY 1)
        tree_sha = commit.tree
        #add to seen tree
        if tree_sha not in seen_tree:
            seen_tree.add(tree_sha)
        #recurse down to get recursive trees inside it and all the blob objects. idk.
        #seems kinda ugly btw
            tree = repo[tree_sha]
            tree_walker(repo,tree,seen_tree,seen_blob)
    #print out the lenghts 
    print("tree_counts: ", len(seen_tree))
    print("blob_counts: ", len(seen_blob))
    print("commits: ", commit_count)

def tree_walker(repo, tree, seen_tree, seen_blob):
    #loop thru all items in the tree
    for name, node, obj_sha in tree.iteritems():
        #find sha256? of all objects
        
        #if the node is a tree node then reucrse
        if node == 16384:
            print('TREE: ', name )
            #obj_sha points to another tree
            if obj_sha not in seen_tree:
                seen_tree.add(obj_sha)
            #get the actual tree object and recurse on it basically.
                obj = repo[obj_sha]
                tree_walker(repo, obj,seen_tree,seen_blob)
        #else its a blob
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