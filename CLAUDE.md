# PackageUpper

PackageUpper is a small package manager for source libraries hosted on GitHub, such as O2Jam and O2Gui. It copies a library's headers, sources and externals into the project that uses it, along with the library's own dependencies.

`README.md` describes how it works: the `packageupper.txt` metadata format, fetching, resolution, the layout of `deps/`, and the commands. Read it before changing behavior; it is the specification the code follows.

## Coding style

The libraries PackageUpper serves are written in C-style C++ that stays plain and explicit, and PackageUpper's Python follows the same spirit: a reader should be able to see what the code does from what is written.

- Plain functions and plain data: dicts, lists, tuples, and `dataclasses` for records. No class hierarchies, metaclasses, decorators beyond `@dataclass`, or other machinery that runs things implicitly
- Standard library only, so the tool runs on a fresh Python install with nothing to `pip install`. The only outside requirement is `git` on the `PATH`
- Python 3.10 or newer
- Windows is the main target and Linux and macOS should keep working: use `pathlib`, call `git` with `subprocess.run` and an argument list, never `shell=True`
- Type hints on function signatures

## Errors and safety

- Report problems with a clear message saying what is wrong and what the user should do, then exit with a non-zero status. No tracebacks for expected failures such as a bad URL, a failed fetch, or a commit conflict
- Errors about a `packageupper.txt` include the file path and line number, as `path:line: message`, so the user can jump to the problem line. Keep the line number with each parsed entry so errors found later, such as a commit conflict, can point back to the line that caused them
- Resolve and fetch everything before touching `deps/`, so a failed run leaves `deps/` exactly as it was
- Never delete or overwrite a `local` dependency
- Treat fetched repositories as data: read their `packageupper.txt` and copy files, never run anything from them

## Documentation

- Any change to the metadata format, the commands, the output layout or user-visible behavior updates `README.md` in the same pull request
- Propose changes to the metadata format or the commands, with their docs, before implementing them

## Testing

- Test against throwaway git repositories created on disk, not against real GitHub repositories, so tests run offline and don't depend on access to private repositories
- Cover the cases that matter most: transitive dependencies, a repository reached twice at the same commit, a commit conflict, `local` dependencies surviving `install` and `clean`, a `local` dependency whose commit conflicts with the tree, and a failed fetch leaving `deps/` untouched

## Workflow

- One feature per branch, with a descriptive branch name, and one pull request per feature
- Ask clarifying questions before designing a feature
