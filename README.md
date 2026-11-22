# PackageUpper

PackageUpper is a small package manager for source libraries hosted on GitHub, such as O2Jam and O2Gui. It copies a library's headers, sources and externals into the project that uses it, along with the library's own dependencies, all the way down the tree.

It is deliberately simple: no version ranges, no registry, no resolver. Every dependency is pinned to an exact commit, and any disagreement about a commit is an error you fix by hand.

PackageUpper lives only in this repository. The repositories it acts on hold just a metadata file; they never contain a copy of the tool.

## Requirements

- Python 3.10 or newer
- `git` on the `PATH`, with access to the repositories you depend on

## Usage

Clone this repository anywhere, then run `packageupper.py` from the project that has a `packageupper.txt`:

```bash
python path/to/PackageUpper/packageupper.py install
```

```bash
python path/to/PackageUpper/packageupper.py clean
```

Both commands take `--project <folder>` to act on a project other than the current folder. Commit `deps/` along with your changes to `packageupper.txt`.

## Metadata file

Every repository PackageUpper acts on has a `packageupper.txt` at its root. It is plain text, one entry per line; blank lines and lines starting with `#` are ignored.

```
# What this repository exports when another project depends on it
include  O2Jam/include
src      O2Jam/src
external externals/BASS

# What a project that depends on this repository must link, and the DLLs it needs
link     externals/BASS/lib/x64/bass.lib  x64
link     externals/BASS/lib/x86/bass.lib  Win32
copy     externals/BASS/lib/x64/bass.dll  x64
copy     externals/BASS/lib/x86/bass.dll  Win32
syslib   winmm.lib

# What this repository depends on
dep      https://github.com/TannerLow/O2Gui/tree/<full commit sha>
local    https://github.com/TannerLow/SomeLib/tree/<full commit sha>
```

- `include`, `src`, `external`: paths, relative to the repository root, that get copied when the repository is used as a dependency. A project that is never depended on, like a game, needs none of them
- `link`: a `.lib` file in the repository that projects depending on it must link, as a path relative to the repository root
- `copy`: a `.dll` file in the repository that must sit next to a depending project's executable
- `syslib`: a `.lib` that comes with Windows, like `winmm.lib`, by name only
- `link`, `copy` and `syslib` take an optional Visual Studio platform after the path: `Win32`, `x64`, `ARM` or `ARM64`. Without one, the line applies to every platform. The paths of `link` and `copy` must be inside an `external` path, since only exported paths are copied, and can't contain spaces. They apply to every project that depends on the repository, directly or through other dependencies
- `dep`: a dependency, as a GitHub tree URL pinned to a full 40-character commit SHA. Branch names and short SHAs are errors
- `local`: a dependency you are editing in place. Its copy in `deps/` is never deleted or overwritten. It still takes part in dependency resolution, using the copy already in `deps/`, including its `packageupper.txt`, so edits to that file count. If it isn't in `deps/` yet, `install` fetches it once. `local` only has an effect in the project's own `packageupper.txt`; in a dependency's file it counts as `dep`

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
- Each dependency is installed into `deps/<repo>`, so two dependencies with the same repository name from different owners are an error
- The same repository at the same commit, reached through two paths, is fetched once
- The same repository at two different commits is an error. The message lists every path that leads to each commit, for example `o2jam@9cdc757 via App` and `o2jam@41af0e2 via App -> O2Gui`, so you know which repository to update. PackageUpper never picks a commit itself
- `local` dependencies are checked the same way, using the commit on their `local` line. If another repository in the tree needs a different commit, that is an error, so a `local` dependency can't silently fall out of date

## Output

Each dependency is copied into its own folder in the project that uses it, under `deps/`. `include` and `src` paths keep their last folder name, and `external` paths go under `externals/`:

```
deps/
    deps.props
    deps.vcxitems
    deps.vcxitems.filters
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
- `deps.props`, `deps.vcxitems` and `deps.vcxitems.filters` are generated for Visual Studio; see below

## Visual Studio

`install` and `clean` write `deps/deps.props`, an MSBuild file that adds every dependency in `deps/` to a Visual Studio project:

- each `include` path, and the `include/` folder of each `external` that has one, on the include path
- every `.c`, `.cc`, `.cpp` and `.cxx` file under each `src` path, compiled
- every `.h`, `.hh`, `.hpp`, `.hxx` and `.inl` file under each `include` and `src` path, listed as a header
- each source file's object file under `$(IntDir)deps\<repo>\...`, keeping its folders, so two `.cpp` files with the same name don't overwrite each other's `.obj`
- every `link` and `syslib` line, linked, for the platform it names
- every `copy` line, copied next to the executable after each build, for the platform it names. Two dependencies that copy a DLL with the same name to the same platform are an error

The file is rebuilt from what is in `deps/` on every run, so files a dependency adds or removes, including in a `local` dependency, are picked up without editing the project. Every file is listed by name, because Visual Studio doesn't support wildcards in project items.

Import it once in the project. In Visual Studio, open **View > Property Manager** (or **View > Other Windows > Property Manager**), right-click the project, choose **Add Existing Property Sheet...**, pick `deps\deps.props`, and save. That adds it to every configuration.

Or edit the `.vcxproj` by hand: add this right after the line that imports `Microsoft.Cpp.props`, with the path from the `.vcxproj` to `deps/`. The condition lets the project load before the first install:

```xml
<Import Project="$(VCTargetsPath)\Microsoft.Cpp.props" />
<Import Project="deps\deps.props" Condition="exists('deps\deps.props')" />
```

- If the project sets its own include directories or libraries, keep `%(AdditionalIncludeDirectories)` and `%(AdditionalDependencies)` in them, which Visual Studio's property pages do by default, so the dependencies' settings still apply
- With `deps.props` alone, the files show up at the top level of the project in Solution Explorer, mixed in with the project's own files: filters live in the project's own `.vcxproj.filters`, which PackageUpper doesn't write. Import `deps.vcxitems` too to keep them apart; see the next section
- Visual Studio may keep using the old file list after `install` or `clean` while the solution is open. If a build looks for files that are gone, unload and reload the project

### Keeping dependency files apart in Solution Explorer

`install` and `clean` also write `deps/deps.vcxitems` and `deps/deps.vcxitems.filters`. Together they are a *shared items project*: Visual Studio's way to share source files between projects. It lists the same files `deps.props` would, sorted into folders that match `deps/`, one top folder per dependency. Solution Explorer shows it as its own `deps` node, so the project's own node holds only its own files.

The files are still compiled as part of the project, the same way: nothing is built separately and there's no extra library to link. `deps.props` keeps the include paths, libraries and DLL copies, and stops listing the files once `deps.vcxitems` is imported, so a project that imports both compiles each file once. A project that doesn't import it keeps working as before.

Set it up once, after the first install:

1. Add it to the solution: right-click the solution in Solution Explorer, choose **Add > Existing Project...**, and pick `deps\deps.vcxitems`
2. Reference it from the project: right-click the project's **References**, choose **Add Shared Project Reference...** (in some versions, **Add Reference...** and its **Shared Projects** tab), tick `deps`, and save

Or edit the `.vcxproj` by hand: put the import in the `ImportGroup` labelled `Shared`, which Visual Studio's templates already have, with the path from the `.vcxproj` to `deps/`:

```xml
<ImportGroup Label="Shared">
  <Import Project="deps\deps.vcxitems" Label="Shared" />
</ImportGroup>
```

- Both files are rewritten on every `install` and `clean`, so files a dependency adds or removes are picked up without editing the project. Their GUIDs come from the project folder's name, so they stay the same from one run to the next
- Don't add files to the `deps` node in Visual Studio: they would go into `deps.vcxitems`, which the next `install` rewrites

## Commands

- `install`: resolves the whole tree, then replaces every non-`local` dependency in `deps/` with a fresh copy, removes dependencies that are no longer in the tree, and rewrites `deps/deps.props`, `deps/deps.vcxitems` and `deps/deps.vcxitems.filters`
- `clean`: deletes every non-`local` dependency from `deps/` and rewrites `deps/deps.props`, `deps/deps.vcxitems` and `deps/deps.vcxitems.filters` with what is left

## Errors

When something is wrong, PackageUpper says what and what to do about it, then exits with a non-zero status. Errors about a `packageupper.txt` name the file and line number. A file in your project is named by its path, for example `deps/O2Gui/packageupper.txt:7: "main" is not a full commit SHA`. A file in a fetched repository is named by the repository and commit it came from, for example `TannerLow/O2Gui@7e040c9/packageupper.txt:7`.

A failed run leaves `deps/` exactly as it was: everything is resolved and fetched before `deps/` is touched.

## Development

Run the tests from the repository root:

```bash
python -m unittest discover tests
```

The tests build throwaway git repositories in a temporary folder and point PackageUpper at them instead of GitHub, so they run offline.
