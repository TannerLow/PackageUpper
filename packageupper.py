"""PackageUpper: copies source libraries from GitHub into a project's deps/ folder.

Usage:
    python packageupper.py install [--project DIR]
    python packageupper.py clean [--project DIR]

README.md describes the metadata format, resolution rules and output layout.
"""

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "packageupper.txt"
DEPS_NAME = "deps"
PROPS_NAME = "deps.props"
ITEMS_NAME = "deps.vcxitems"
ITEMS_FILTERS_NAME = "deps.vcxitems.filters"
# Set by deps.vcxitems. deps.props lists the files itself only when it isn't
# set, so a project that imports both doesn't get every file twice.
SHARED_ITEMS_PROPERTY = "PackageUpperSharedItems"
GITHUB = "https://github.com"

URL_PATTERN = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/tree/([^/\s]+)/?$"
)
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

EXPORT_KINDS = ("include", "src", "external")
DEP_KINDS = ("dep", "local")
BUILD_KINDS = ("link", "copy", "syslib")

# Visual Studio platform names a link, copy or syslib line can be limited to
PLATFORMS = ("Win32", "x64", "ARM", "ARM64")

# File types deps.props lists for Visual Studio
SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx")
HEADER_EXTENSIONS = (".h", ".hh", ".hpp", ".hxx", ".inl")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Export:
    kind: str          # "include", "src" or "external"
    path: str          # relative to the repository root, using "/"
    line: int


@dataclass
class DepRef:
    kind: str          # "dep" or "local"
    owner: str
    repo: str
    sha: str           # lowercase, 40 characters
    line: int


@dataclass
class BuildEntry:
    kind: str          # "link", "copy" or "syslib"
    value: str         # for link and copy, a path relative to the repository root; for syslib, a library name
    platform: str      # a name from PLATFORMS, or "" for every platform
    line: int


@dataclass
class Manifest:
    display_path: str  # how errors name this file
    exports: list = field(default_factory=list)  # list of Export
    deps: list = field(default_factory=list)     # list of DepRef
    build: list = field(default_factory=list)    # list of BuildEntry


@dataclass
class Reference:
    """One place in the tree that asks for a repository at a commit."""
    sha: str
    chain: list        # names from the project down to the repository that asked
    display_path: str  # the manifest that holds the line
    line: int


@dataclass
class Package:
    key: str           # "owner/repo", lowercase
    owner: str
    repo: str          # spelling used for the deps/ folder name
    sha: str
    is_local: bool
    source_dir: Path   # where the files to install are: a fetched checkout, or deps/<repo> for local
    manifest: Manifest


class UserError(Exception):
    """An expected failure, reported to the user without a traceback."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_dep_url(url: str, display_path: str, line: int) -> tuple:
    match = URL_PATTERN.match(url)
    if match is None:
        raise UserError(
            f"{display_path}:{line}: \"{url}\" is not a GitHub tree URL. "
            f"Expected https://github.com/<owner>/<repo>/tree/<full commit sha>"
        )
    owner = match.group(1)
    repo = match.group(2)
    sha = match.group(3)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if SHA_PATTERN.match(sha) is None:
        raise UserError(
            f"{display_path}:{line}: \"{sha}\" is not a full commit SHA. "
            f"Use the full 40-character SHA of the commit, not a branch name or short SHA"
        )
    return owner, repo, sha.lower()


def check_export_path(path: str, display_path: str, line: int) -> str:
    normalized = path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if normalized == "" or path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise UserError(
            f"{display_path}:{line}: \"{path}\" must be a path relative to the repository root"
        )
    if ".." in parts or "." in parts:
        raise UserError(
            f"{display_path}:{line}: \"{path}\" must not contain \".\" or \"..\""
        )
    return normalized


def export_target(kind: str, path: str) -> str:
    """Where an export goes inside deps/<repo>: include/ and src/ keep their last
    folder name, externals go under externals/."""
    name = path.split("/")[-1]
    if kind == "external":
        return "externals/" + name
    return name


def parse_build_entry(
    kind: str,
    value: str,
    display_path: str,
    line: int
) -> BuildEntry:
    words = value.split()
    if len(words) > 2:
        raise UserError(
            f"{display_path}:{line}: \"{kind}\" takes a path and an optional platform. "
            f"Paths with spaces aren't supported here"
        )
    path = words[0]
    platform = ""
    if len(words) == 2:
        platform = words[1]
        if platform not in PLATFORMS:
            hint = ""
            if platform.lower() in ("x86", "win32"):
                hint = " Use Win32 for 32-bit builds."
            raise UserError(
                f"{display_path}:{line}: unknown platform \"{platform}\". "
                f"Expected one of: {', '.join(PLATFORMS)}.{hint}"
            )

    if kind == "syslib":
        if "/" in path or "\\" in path or not path.lower().endswith(".lib"):
            raise UserError(
                f"{display_path}:{line}: \"{path}\" should be the name of a .lib that comes with "
                f"Windows, like winmm.lib. Use \"link\" for a .lib in the repository"
            )
        return BuildEntry(kind=kind, value=path, platform=platform, line=line)

    path = check_export_path(path, display_path, line)
    extension = ".lib" if kind == "link" else ".dll"
    if not path.lower().endswith(extension):
        raise UserError(f"{display_path}:{line}: \"{kind}\" takes a {extension} file, not \"{path}\"")
    return BuildEntry(kind=kind, value=path, platform=platform, line=line)


def find_external(
    manifest: Manifest,
    path: str
) -> Export:
    """The external export that contains path, or None."""
    for export in manifest.exports:
        if export.kind == "external" and (path == export.path or path.startswith(export.path + "/")):
            return export
    return None


def path_in_deps(
    manifest: Manifest,
    path: str
) -> str:
    """Where a link or copy path from the repository ends up inside deps/<repo>."""
    export = find_external(manifest, path)
    return export_target(export.kind, export.path) + path[len(export.path):]


def parse_manifest(text: str, display_path: str) -> Manifest:
    manifest = Manifest(display_path=display_path)
    export_names = {}
    line_number = 0
    for raw_line in text.splitlines():
        line_number += 1
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue

        words = stripped.split(None, 1)
        kind = words[0]
        if len(words) < 2:
            raise UserError(f"{display_path}:{line_number}: \"{kind}\" needs a value after it")
        value = words[1].strip()

        if kind in EXPORT_KINDS:
            path = check_export_path(value, display_path, line_number)
            target = export_target(kind, path).lower()
            if target in export_names or target == MANIFEST_NAME:
                raise UserError(
                    f"{display_path}:{line_number}: \"{path}\" would be copied to \"{target}\", "
                    f"the same place as line {export_names.get(target)}. Exports need different names"
                )
            export_names[target] = line_number
            manifest.exports.append(Export(kind=kind, path=path, line=line_number))
        elif kind in DEP_KINDS:
            owner, repo, sha = parse_dep_url(value, display_path, line_number)
            manifest.deps.append(
                DepRef(kind=kind, owner=owner, repo=repo, sha=sha, line=line_number)
            )
        elif kind in BUILD_KINDS:
            manifest.build.append(parse_build_entry(kind, value, display_path, line_number))
        else:
            raise UserError(
                f"{display_path}:{line_number}: unknown entry \"{kind}\". "
                f"Expected one of: {', '.join(EXPORT_KINDS + DEP_KINDS + BUILD_KINDS)}"
            )

    # Checked after the whole file, so link and copy lines can come before their external line
    for entry in manifest.build:
        if entry.kind != "syslib" and find_external(manifest, entry.value) is None:
            raise UserError(
                f"{display_path}:{entry.line}: \"{entry.value}\" is not inside an exported external. "
                f"Add an \"external\" line for the folder that holds it"
            )
    return manifest


def read_manifest(path: Path, display_path: str) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise UserError(f"{display_path}: file not found")
    except UnicodeDecodeError:
        raise UserError(f"{display_path}: not a UTF-8 text file")
    return parse_manifest(text, display_path)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def make_writable_and_retry(function, path, exc_info) -> None:
    # Git marks its object files read-only, which stops rmtree on Windows.
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


def copy_exports(package: Package, destination: Path) -> None:
    destination.mkdir(parents=True)
    shutil.copy2(package.source_dir / MANIFEST_NAME, destination / MANIFEST_NAME)
    root = package.source_dir.resolve()
    for export in package.manifest.exports:
        source = package.source_dir / export.path
        target = destination / export_target(export.kind, export.path)
        # Fetched repositories are data: an export must not lead outside the repository.
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise UserError(
                f"{package.manifest.display_path}:{export.line}: \"{export.path}\" "
                f"is a symbolic link or leads outside the repository"
            )
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise UserError(
                f"{package.manifest.display_path}:{export.line}: \"{export.path}\" "
                f"does not exist in {package.owner}/{package.repo} at commit {package.sha[:7]}"
            )


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def fetch_commit(
    git_host: str,
    owner: str,
    repo: str,
    sha: str,
    destination: Path
) -> None:
    url = f"{git_host}/{owner}/{repo}"
    commands = [
        ["git", "init", "--quiet", str(destination)],
        ["git", "-C", str(destination), "fetch", "--quiet", "--depth", "1", url, sha],
        ["git", "-C", str(destination), "-c", "advice.detachedHead=false",
         "checkout", "--quiet", "FETCH_HEAD"],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise UserError("git was not found. Install git and make sure it is on the PATH")
        if result.returncode != 0:
            raise UserError(
                f"could not fetch {owner}/{repo} at commit {sha[:7]} from {url}\n"
                f"{result.stderr.strip()}\n"
                f"Check that the repository exists, that the commit is pushed, "
                f"and that git can access it (try: git clone {url})"
            )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def format_reference(key: str, reference: Reference) -> str:
    return (
        f"  {key}@{reference.sha[:7]} via {' -> '.join(reference.chain)} "
        f"({reference.display_path}:{reference.line})"
    )


def resolve(
    project_dir: Path,
    fetch_dir: Path,
    git_host: str,
    log
) -> list:
    """Walks the dependency tree and returns the list of Package, without touching deps/.

    Non-local packages are fetched into fetch_dir. Local packages are read from deps/,
    or fetched if they are not there yet."""
    deps_dir = project_dir / DEPS_NAME
    root = read_manifest(project_dir / MANIFEST_NAME, MANIFEST_NAME)
    root_name = project_dir.resolve().name

    local_keys = set()
    for ref in root.deps:
        if ref.kind == "local":
            local_keys.add(f"{ref.owner}/{ref.repo}".lower())

    packages = {}      # key -> Package
    references = {}    # key -> list of Reference
    folder_owners = {} # lowercase deps/ folder name -> key
    conflicts = []     # keys with more than one commit

    # Breadth-first, so the project's own lines are seen first.
    queue = [(root, [root_name])]
    while len(queue) > 0:
        manifest, chain = queue.pop(0)
        for ref in manifest.deps:
            key = f"{ref.owner}/{ref.repo}".lower()
            reference = Reference(
                sha=ref.sha, chain=chain, display_path=manifest.display_path, line=ref.line
            )
            if key not in references:
                references[key] = []
            references[key].append(reference)

            if key in packages:
                if packages[key].sha != ref.sha and key not in conflicts:
                    conflicts.append(key)
                continue

            folder = ref.repo.lower()
            if folder in folder_owners:
                other = folder_owners[folder]
                raise UserError(
                    f"{manifest.display_path}:{ref.line}: {ref.owner}/{ref.repo} and {other} "
                    f"would both be installed into {DEPS_NAME}/{ref.repo}. "
                    f"Two dependencies can't share a repository name"
                )
            folder_owners[folder] = key

            is_local = key in local_keys
            local_dir = deps_dir / ref.repo
            if is_local and (local_dir / MANIFEST_NAME).is_file():
                source_dir = local_dir
                display_path = f"{DEPS_NAME}/{ref.repo}/{MANIFEST_NAME}"
            else:
                source_dir = fetch_dir / key.replace("/", "__")
                display_path = f"{ref.owner}/{ref.repo}@{ref.sha[:7]}/{MANIFEST_NAME}"
                log(f"Fetching {ref.owner}/{ref.repo}@{ref.sha[:7]}")
                fetch_commit(git_host, ref.owner, ref.repo, ref.sha, source_dir)
                if not (source_dir / MANIFEST_NAME).is_file():
                    raise UserError(
                        f"{manifest.display_path}:{ref.line}: {ref.owner}/{ref.repo} has no "
                        f"{MANIFEST_NAME} at its root at commit {ref.sha[:7]}"
                    )

            dep_manifest = read_manifest(source_dir / MANIFEST_NAME, display_path)
            packages[key] = Package(
                key=key,
                owner=ref.owner,
                repo=ref.repo,
                sha=ref.sha,
                is_local=is_local,
                source_dir=source_dir,
                manifest=dep_manifest,
            )
            # A local line only means something in the project's own file.
            queue.append((dep_manifest, chain + [ref.repo]))

    if len(conflicts) > 0:
        lines = ["dependencies need different commits of the same repository:"]
        for key in conflicts:
            for reference in references[key]:
                lines.append(format_reference(key, reference))
        lines.append("Update the lines above so every repository asks for the same commit")
        raise UserError("\n".join(lines))

    return list(packages.values())


# ---------------------------------------------------------------------------
# Visual Studio
# ---------------------------------------------------------------------------

def msbuild_escape(text: str) -> str:
    """Escapes a path for an MSBuild attribute or element: XML first, then
    MSBuild's own special characters."""
    text = text.replace("%", "%25")
    for character, code in (("$", "%24"), ("@", "%40"), (";", "%3B"), ("'", "%27"), ("*", "%2A"), ("?", "%3F")):
        text = text.replace(character, code)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return text


def windows_path(relative: str) -> str:
    return relative.replace("/", "\\")


def files_with_extensions(
    folder: Path,
    extensions: tuple
) -> list:
    found = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            found.append(path)
    return found


def build_props(
    folders: list,
    project_name: str
) -> tuple:
    """Builds the text of deps/deps.props, deps/deps.vcxitems and
    deps/deps.vcxitems.filters. folders is a list of (name, source_dir,
    display_path): each dependency's folder name in deps/, where its files are
    right now, which is a staging folder during install, and how errors name
    its packageupper.txt. project_name is the project folder's name, which
    makes the shared items project's GUID. Returns (texts, summary), where
    texts maps each file name to its text.

    Raises UserError for a link or copy file that is missing, or two DLLs that
    would be copied to the same name, so install can stop before touching deps/."""
    include_dirs = []  # paths relative to deps/, using "/"
    sources = []
    headers = []
    links = {}         # platform ("" for every platform) -> list of values for AdditionalDependencies
    copies = {}        # platform -> list of paths relative to deps/
    copy_names = []    # (file name, platform, display_path, line), to find two DLLs with the same name

    for name, source_dir, display_path in folders:
        manifest = read_manifest(source_dir / MANIFEST_NAME, display_path)
        for export in manifest.exports:
            target_name = export_target(export.kind, export.path)
            target = source_dir / target_name
            if not target.is_dir():
                continue
            if export.kind == "include":
                include_dirs.append(f"{name}/{target_name}")
                for path in files_with_extensions(target, HEADER_EXTENSIONS):
                    headers.append(f"{name}/{path.relative_to(source_dir).as_posix()}")
            elif export.kind == "src":
                for path in files_with_extensions(target, SOURCE_EXTENSIONS):
                    sources.append(f"{name}/{path.relative_to(source_dir).as_posix()}")
                for path in files_with_extensions(target, HEADER_EXTENSIONS):
                    headers.append(f"{name}/{path.relative_to(source_dir).as_posix()}")
            elif export.kind == "external":
                # An external's headers are found by the include/ folder convention
                if (target / "include").is_dir():
                    include_dirs.append(f"{name}/{target_name}/include")

        for entry in manifest.build:
            if entry.platform not in links:
                links[entry.platform] = []
                copies[entry.platform] = []
            if entry.kind == "syslib":
                links[entry.platform].append(msbuild_escape(entry.value))
                continue
            inside = path_in_deps(manifest, entry.value)
            if not (source_dir / inside).is_file():
                raise UserError(f"{display_path}:{entry.line}: \"{entry.value}\" does not exist")
            if entry.kind == "link":
                links[entry.platform].append(here_path(f"{name}/{inside}"))
            else:
                file_name = inside.split("/")[-1].lower()
                for other_name, other_platform, other_path, other_line in copy_names:
                    overlap = entry.platform == "" or other_platform == "" or entry.platform == other_platform
                    if other_name == file_name and overlap:
                        raise UserError(
                            f"{display_path}:{entry.line}: \"{file_name}\" would be copied next to the "
                            f"executable by {other_path}:{other_line} too. Only one dependency can provide it"
                        )
                copy_names.append((file_name, entry.platform, display_path, entry.line))
                copies[entry.platform].append(f"{name}/{inside}")

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f"<!-- Generated by PackageUpper from {DEPS_NAME}/. Don't edit: install and clean rewrite it. -->",
        '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">',
        "  <ItemDefinitionGroup>",
        "    <ClCompile>",
    ]
    include_value = ""
    for folder in include_dirs:
        include_value += here_path(folder) + ";"
    lines.append(f"      <AdditionalIncludeDirectories>{include_value}%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>")
    lines.append("    </ClCompile>")
    lines.append("  </ItemDefinitionGroup>")

    # "" (every platform) first, then each platform by name
    platforms = sorted(links.keys())
    for platform in platforms:
        if len(links[platform]) == 0:
            continue
        lines.append(f"  <ItemDefinitionGroup{platform_condition(platform)}>")
        lines.append("    <Link>")
        lines.append(f"      <AdditionalDependencies>{';'.join(links[platform])};%(AdditionalDependencies)</AdditionalDependencies>")
        lines.append("    </Link>")
        lines.append("  </ItemDefinitionGroup>")

    # The files, for a project that doesn't import deps.vcxitems
    lines.append(f"  <ItemGroup Condition=\"'$({SHARED_ITEMS_PROPERTY})'!='true'\">")
    lines.extend(file_item_lines(sources, headers))
    lines.append("  </ItemGroup>")

    copy_count = 0
    for platform in platforms:
        if len(copies[platform]) == 0:
            continue
        lines.append(f"  <ItemGroup{platform_condition(platform)}>")
        for path in copies[platform]:
            lines.append(f'    <PackageUpperCopy Include="{here_path(path)}" />')
            copy_count += 1
        lines.append("  </ItemGroup>")
    lines.append("  <!-- Copies the dependencies' DLLs next to the executable after each build -->")
    lines.append('  <Target Name="PackageUpperCopy" AfterTargets="Build">')
    lines.append('    <Copy SourceFiles="@(PackageUpperCopy)" DestinationFolder="$(OutDir)" SkipUnchangedFiles="true" />')
    lines.append("  </Target>")
    lines.append("</Project>")

    link_count = 0
    for platform in platforms:
        link_count += len(links[platform])
    summary = (
        f"{len(sources)} source files, {len(include_dirs)} include folders, "
        f"{link_count} libraries, {copy_count} DLLs"
    )
    texts = {
        PROPS_NAME: "\n".join(lines) + "\n",
        ITEMS_NAME: build_items(sources, headers, project_name),
        ITEMS_FILTERS_NAME: build_items_filters(sources, headers),
    }
    return texts, summary


def file_item_lines(
    sources: list,
    headers: list
) -> list:
    """The ClCompile and ClInclude items for the dependencies' files, the same
    in deps.props and deps.vcxitems."""
    lines = []
    for source in sources:
        # Object files keep their folders, so two .cpp files with the same name don't
        # overwrite each other's .obj. Set per file, because these paths are absolute.
        folder = source.rsplit("/", 1)[0]
        object_dir = f"$(IntDir){DEPS_NAME}\\" + msbuild_escape(windows_path(folder)) + "\\"
        lines.append(f'    <ClCompile Include="{here_path(source)}">')
        lines.append(f"      <ObjectFileName>{object_dir}</ObjectFileName>")
        lines.append("    </ClCompile>")
    for header in headers:
        lines.append(f'    <ClInclude Include="{here_path(header)}" />')
    return lines


def stable_guid(text: str) -> str:
    """A GUID that is the same on every run for the same text, so regenerating
    a file doesn't change the GUIDs Visual Studio has stored."""
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "packageupper:" + text.lower())).upper() + "}"


def build_items(
    sources: list,
    headers: list,
    project_name: str
) -> str:
    """Builds deps/deps.vcxitems: a Visual Studio shared items project that
    lists the dependencies' files, so Solution Explorer shows them under their
    own node instead of mixed in with the project's files."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f"<!-- Generated by PackageUpper from {DEPS_NAME}/. Don't edit: install and clean rewrite it. -->",
        '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">',
        '  <PropertyGroup Label="Globals">',
        "    <HasSharedItems>true</HasSharedItems>",
        f"    <ItemsProjectGuid>{stable_guid('items:' + project_name)}</ItemsProjectGuid>",
        "    <ItemsProjectName>deps</ItemsProjectName>",
        "  </PropertyGroup>",
        "  <PropertyGroup>",
        f"    <{SHARED_ITEMS_PROPERTY}>true</{SHARED_ITEMS_PROPERTY}>",
        "  </PropertyGroup>",
        "  <ItemGroup>",
        '    <ProjectCapability Include="SourceItemsFromImports" />',
        "  </ItemGroup>",
        "  <ItemGroup>",
    ]
    lines.extend(file_item_lines(sources, headers))
    lines.append("  </ItemGroup>")
    lines.append("</Project>")
    return "\n".join(lines) + "\n"


def build_items_filters(
    sources: list,
    headers: list
) -> str:
    """Builds deps/deps.vcxitems.filters, which puts each file in Solution
    Explorer folders that match its folders in deps/, one top folder per
    dependency."""
    filters = set()
    for path in sources + headers:
        parts = path.split("/")[:-1]
        for count in range(1, len(parts) + 1):
            filters.add("/".join(parts[:count]))

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f"<!-- Generated by PackageUpper from {DEPS_NAME}/. Don't edit: install and clean rewrite it. -->",
        '<Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">',
        "  <ItemGroup>",
    ]
    for folder in sorted(filters, key=str.lower):
        lines.append(f'    <Filter Include="{msbuild_escape(windows_path(folder))}">')
        lines.append(f"      <UniqueIdentifier>{stable_guid('filter:' + folder)}</UniqueIdentifier>")
        lines.append("    </Filter>")
    lines.append("  </ItemGroup>")
    lines.append("  <ItemGroup>")
    for kind, paths in (("ClCompile", sources), ("ClInclude", headers)):
        for path in paths:
            folder = path.rsplit("/", 1)[0]
            lines.append(f'    <{kind} Include="{here_path(path)}">')
            lines.append(f"      <Filter>{msbuild_escape(windows_path(folder))}</Filter>")
            lines.append(f"    </{kind}>")
    lines.append("  </ItemGroup>")
    lines.append("</Project>")
    return "\n".join(lines) + "\n"


def here_path(relative: str) -> str:
    """A path relative to deps/, as deps.props refers to it."""
    return "$(MSBuildThisFileDirectory)" + msbuild_escape(windows_path(relative))


def platform_condition(platform: str) -> str:
    if platform == "":
        return ""
    return f" Condition=\"'$(Platform)'=='{platform}'\""


def write_props(
    deps_dir: Path,
    texts: dict,
    summary: str,
    log
) -> None:
    for name, text in texts.items():
        # newline="\n" so the file is the same whichever OS ran the install
        (deps_dir / name).write_text(text, encoding="utf-8", newline="\n")
    log(f"Wrote {DEPS_NAME}/{PROPS_NAME}, {ITEMS_NAME} and {ITEMS_FILTERS_NAME} ({summary})")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def package_sort_key(package: Package) -> str:
    return package.repo.lower()


def folder_sort_key(folder: Path) -> str:
    return folder.name.lower()


def local_folder_names(project_dir: Path) -> set:
    root = read_manifest(project_dir / MANIFEST_NAME, MANIFEST_NAME)
    names = set()
    for ref in root.deps:
        if ref.kind == "local":
            names.add(ref.repo.lower())
    return names


def install(
    project_dir: Path,
    git_host: str = GITHUB,
    log=print
) -> None:
    deps_dir = project_dir / DEPS_NAME
    fetch_dir = Path(tempfile.mkdtemp(prefix="packageupper-"))
    try:
        packages = resolve(project_dir, fetch_dir, git_host, log)

        # Copy every package to a staging folder first, so a missing export or a
        # copy error stops the run before deps/ is touched.
        staging_dir = fetch_dir / "staging"
        staging_dir.mkdir()
        for package in packages:
            if package.is_local and package.source_dir == deps_dir / package.repo:
                continue
            copy_exports(package, staging_dir / package.repo)

        # Built before deps/ is touched, so its errors also leave deps/ as it was
        folders = []
        for package in sorted(packages, key=package_sort_key):
            if package.is_local and package.source_dir == deps_dir / package.repo:
                folders.append((package.repo, package.source_dir, package.manifest.display_path))
            else:
                folders.append((package.repo, staging_dir / package.repo, package.manifest.display_path))
        props_texts, props_summary = build_props(folders, project_dir.resolve().name)

        deps_dir.mkdir(exist_ok=True)
        wanted = set()
        for package in packages:
            wanted.add(package.repo.lower())

        for entry in sorted(deps_dir.iterdir()):
            if entry.is_dir() and entry.name.lower() not in wanted:
                log(f"Removing {DEPS_NAME}/{entry.name}")
                remove_tree(entry)

        for package in packages:
            target = deps_dir / package.repo
            if package.is_local and package.source_dir == target:
                log(f"Keeping local {DEPS_NAME}/{package.repo}")
                continue
            for entry in deps_dir.iterdir():
                if entry.is_dir() and entry.name.lower() == package.repo.lower():
                    remove_tree(entry)
            shutil.move(str(staging_dir / package.repo), str(target))
            log(f"Installed {DEPS_NAME}/{package.repo} ({package.sha[:7]})")

        write_props(deps_dir, props_texts, props_summary, log)
    finally:
        remove_tree(fetch_dir)


def clean(
    project_dir: Path,
    log=print
) -> None:
    deps_dir = project_dir / DEPS_NAME
    local_names = local_folder_names(project_dir)
    if not deps_dir.is_dir():
        return

    # deps.props and deps.vcxitems for what will be left, built first so its errors leave deps/ as it was
    kept = []
    for entry in sorted(deps_dir.iterdir(), key=folder_sort_key):
        if entry.is_dir() and entry.name.lower() in local_names and (entry / MANIFEST_NAME).is_file():
            kept.append((entry.name, entry, f"{DEPS_NAME}/{entry.name}/{MANIFEST_NAME}"))
    props_texts, props_summary = build_props(kept, project_dir.resolve().name)

    for entry in sorted(deps_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.lower() in local_names:
            log(f"Keeping local {DEPS_NAME}/{entry.name}")
            continue
        log(f"Removing {DEPS_NAME}/{entry.name}")
        remove_tree(entry)

    write_props(deps_dir, props_texts, props_summary, log)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="packageupper",
        description="Copies source libraries from GitHub into a project's deps/ folder.",
    )
    parser.add_argument("command", choices=["install", "clean"])
    parser.add_argument(
        "--project",
        default=".",
        help="the project folder, which holds packageupper.txt (default: the current folder)",
    )
    args = parser.parse_args(argv)

    project_dir = Path(args.project)
    if not project_dir.is_dir():
        print(f"error: {project_dir} is not a folder", file=sys.stderr)
        return 1

    try:
        if args.command == "install":
            install(project_dir)
        else:
            clean(project_dir)
    except UserError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print("error: PackageUpper needs Python 3.10 or newer", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
