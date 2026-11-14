# PackageUpper

PackageUpper is a small package manager for source libraries hosted on GitHub, such as O2Jam and O2Gui. It copies a library's headers, sources and externals into the project that uses it, along with the library's own dependencies, all the way down the tree.

It is deliberately simple: no version ranges, no registry, no resolver. Every dependency is pinned to an exact commit, and any disagreement about a commit is an error you fix by hand.

PackageUpper lives only in this repository. The repositories it acts on hold just a metadata file; they never contain a copy of the tool.

## Requirements

- Python 3.10 or newer
- `git` on the `PATH`, with access to the repositories you depend on

## Metadata file

Every repository PackageUpper acts on has a `packageupper.txt` at its root. It is plain text, one entry per line; blank lines and lines starting with `#` are ignored.

```
# What this repository exports when another project depends on it
include  O2Jam/include
src      O2Jam/src
external O2Jam/externals/BASS

# What this repository depends on
dep      https://github.com/TannerLow/O2Gui/tree/<full commit sha>
local    https://github.com/TannerLow/SomeLib/tree/<full commit sha>
```

- `include`, `src`, `external`: paths, relative to the repository root, that get copied when the repository is used as a dependency. A project that is never depended on, like a game, needs none of them
- `dep`: a dependency, as a GitHub tree URL pinned to a full 40-character commit SHA. Branch names and short SHAs are errors
- `local`: a dependency you are editing in place. Its copy in `deps/` is never deleted or overwritten. It still takes part in dependency resolution, using the copy already in `deps/`

## Fetching

PackageUpper fetches with the `git` command line, never with the GitHub API or raw file URLs:

```bash
git init <temp dir>
git -C <temp dir> fetch --depth 1 https://github.com/<owner>/<repo> <sha>
git -C <temp dir> checkout FETCH_HEAD
```

This way private repositories work with whatever credentials git already has on the machine (Git Credential Manager, SSH keys), and PackageUpper never handles tokens. Git fetches the whole repository at that commit into a temporary directory; only the exported paths are copied out of it, and the temporary directory is deleted afterwards.

## Resolution

- Repositories are identified by `owner/repo`, lowercased, because GitHub ignores case
- The same repository at the same commit, reached through two paths, is fetched once
- The same repository at two different commits is an error. The message lists every path that leads to each commit, for example `o2jam@9cdc757 via App` and `o2jam@41af0e2 via App -> O2Gui`, so you know which repository to update. PackageUpper never picks a commit itself
- `local` dependencies are checked the same way, using the commit on their `local` line. If another repository in the tree needs a different commit, that is an error, so a `local` dependency can't silently fall out of date

## Output

Each dependency is copied into its own folder in the project that uses it, under `deps/`, keeping the exported paths' last folder names:

```
deps/
    O2Jam/
        packageupper.txt
        include/o2/jam/...
        src/o2/jam/...
        externals/BASS/...
    O2Gui/
        ...
```

- Each dependency's `packageupper.txt` is copied too, so `local` dependencies can be resolved without fetching
- `deps/` is meant to be committed to the project's repository, so the project builds and can be read without running PackageUpper
- PackageUpper owns `deps/` and nothing else in the project: it never writes outside `deps/`

## Commands

- `install`: resolves the whole tree, then replaces every non-`local` dependency in `deps/` with a fresh copy and removes dependencies that are no longer in the tree
- `clean`: deletes every non-`local` dependency from `deps/`

## Errors

When something is wrong, PackageUpper says what and what to do about it, then exits with a non-zero status. Errors about a `packageupper.txt` name the file and line number, for example `deps/O2Gui/packageupper.txt:7: "tree/main" is not a full commit SHA`.

A failed run leaves `deps/` exactly as it was: everything is resolved and fetched before `deps/` is touched.
