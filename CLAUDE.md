# PackageUpper

PackageUpper is a small package manager for source libraries hosted on GitHub, such as O2Jam and O2Gui. It copies a library's headers, sources and externals into the project that uses it, along with the library's own dependencies, all the way down the tree.

It is deliberately simple: no version ranges, no registry, no resolver. Every dependency is pinned to an exact commit, and any disagreement about a commit is an error the user fixes by hand.

PackageUpper lives only in this repository. The repositories it acts on hold just a metadata file; they never contain a copy of the tool.

## How it works

### Metadata file

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
- `local`: a dependency the user is editing in place. Its copy in `deps/` is never deleted or overwritten. It still takes part in dependency resolution, using the copy already in `deps/`

### Fetching

PackageUpper fetches with the `git` command line, never with the GitHub API or raw file URLs:

```bash
git init <temp dir>
git -C <temp dir> fetch --depth 1 https://github.com/<owner>/<repo> <sha>
git -C <temp dir> checkout FETCH_HEAD
```

This way private repositories work with whatever credentials git already has on the machine (Git Credential Manager, SSH keys), and PackageUpper never handles tokens. Git fetches the whole repository at that commit into a temporary directory; only the exported paths are copied out of it, and the temporary directory is deleted afterwards.

### Resolution

- Repositories are identified by `owner/repo`, lowercased, because GitHub ignores case
- The same repository at the same commit, reached through two paths, is fetched once
- The same repository at two different commits is an error. The message lists every path that leads to each commit, for example `o2jam@9cdc757 via App` and `o2jam@41af0e2 via App -> O2Gui`, so the user knows which repository to update. PackageUpper never picks a commit itself

### Output

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

### Commands

- `install`: resolves the whole tree, then replaces every non-`local` dependency in `deps/` with a fresh copy and removes dependencies that are no longer in the tree
- `clean`: deletes every non-`local` dependency from `deps/`

## Coding style

The libraries PackageUpper serves are written in C-style C++ that stays plain and explicit, and PackageUpper's Python follows the same spirit: a reader should be able to see what the code does from what is written.

- Plain functions and plain data: dicts, lists, tuples, and `dataclasses` for records. No class hierarchies, metaclasses, decorators beyond `@dataclass`, or other machinery that runs things implicitly
- Standard library only, so the tool runs on a fresh Python install with nothing to `pip install`. The only outside requirement is `git` on the `PATH`
- Python 3.10 or newer
- Windows is the main target and Linux and macOS should keep working: use `pathlib`, call `git` with `subprocess.run` and an argument list, never `shell=True`
- Type hints on function signatures

## Errors and safety

- Report problems with a clear message saying what is wrong and what the user should do, then exit with a non-zero status. No tracebacks for expected failures such as a bad URL, a failed fetch, or a commit conflict
- Resolve and fetch everything before touching `deps/`, so a failed run leaves `deps/` exactly as it was
- Never delete or overwrite a `local` dependency
- Treat fetched repositories as data: read their `packageupper.txt` and copy files, never run anything from them

## Documentation

`README.md` documents how to use PackageUpper: the metadata file format, the commands, and the layout of `deps/`.
- Any change to the metadata format, the commands or the output layout updates `README.md` in the same pull request
- Propose changes to the metadata format or the commands, with their docs, before implementing them

## Testing

- Test against throwaway git repositories created on disk, not against real GitHub repositories, so tests run offline and don't depend on access to private repositories
- Cover the cases that matter most: transitive dependencies, a repository reached twice at the same commit, a commit conflict, `local` dependencies surviving `install` and `clean`, and a failed fetch leaving `deps/` untouched

## Workflow

- One feature per branch, with a descriptive branch name, and one pull request per feature
- Ask clarifying questions before designing a feature
