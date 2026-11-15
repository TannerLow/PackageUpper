"""Tests for packageupper.py, run with: python -m unittest discover tests

Each test builds throwaway git repositories on disk and points PackageUpper at
them instead of GitHub, so the tests run offline.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import packageupper  # noqa: E402

OWNER = "tester"


def git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir),
         "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    return result.stdout.strip()


def write_files(directory: Path, files: dict) -> None:
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def url(name: str, sha: str) -> str:
    return f"https://github.com/{OWNER}/{name}/tree/{sha}"


class PackageUpperTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="packageupper-test-"))
        self.hosts = self.temp / "hosts"
        self.project = self.temp / "App"
        self.project.mkdir()
        self.git_host = self.hosts.as_uri()
        self.messages = []

    def tearDown(self) -> None:
        packageupper.remove_tree(self.temp)

    # Helpers ---------------------------------------------------------------

    def commit(self, name: str, files: dict) -> str:
        """Commits files to the fake GitHub repository tester/<name> and returns the SHA."""
        repo_dir = self.hosts / OWNER / name
        if not repo_dir.exists():
            repo_dir.mkdir(parents=True)
            git(repo_dir, "init", "--quiet")
            git(repo_dir, "config", "uploadpack.allowAnySHA1InWant", "true")
        write_files(repo_dir, files)
        git(repo_dir, "add", "--all")
        git(repo_dir, "commit", "--quiet", "--allow-empty", "-m", "commit")
        return git(repo_dir, "rev-parse", "HEAD")

    def library(self, name: str, deps: list = None) -> str:
        """Commits a library with include/, src/ and externals/ and returns the SHA."""
        lines = [
            f"include {name}/include",
            f"src     {name}/src",
            f"external externals/{name}Ext",
        ]
        for dep in deps or []:
            lines.append(dep)
        return self.commit(name, {
            "packageupper.txt": "\n".join(lines) + "\n",
            f"{name}/include/o2/{name.lower()}/{name.lower()}.h": f"// {name} header\n",
            f"{name}/src/o2/{name.lower()}/{name.lower()}.cpp": f"// {name} source\n",
            f"externals/{name}Ext/lib/{name.lower()}.lib": "binary\n",
            f"{name}/main.cpp": "// demo, not exported\n",
        })

    def set_project(self, lines: list) -> None:
        (self.project / "packageupper.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def install(self) -> None:
        packageupper.install(self.project, git_host=self.git_host, log=self.messages.append)

    def clean(self) -> None:
        packageupper.clean(self.project, log=self.messages.append)

    def deps(self) -> Path:
        return self.project / "deps"

    def dep_folders(self) -> list:
        return sorted(p.name for p in self.deps().iterdir() if p.is_dir())

    def props(self) -> str:
        return (self.deps() / "deps.props").read_text(encoding="utf-8")

    def snapshot(self) -> dict:
        files = {}
        for path in sorted(self.deps().rglob("*")):
            if path.is_file():
                files[path.relative_to(self.deps()).as_posix()] = path.read_text(encoding="utf-8")
        return files

    def assert_error(self, *expected: str) -> str:
        with self.assertRaises(packageupper.UserError) as context:
            self.install()
        message = str(context.exception)
        for text in expected:
            self.assertIn(text, message)
        return message

    # Tests -----------------------------------------------------------------

    def test_installs_exports_and_transitive_dependencies(self) -> None:
        sha_b = self.library("B")
        sha_a = self.library("A", [f"dep {url('B', sha_b)}"])
        self.set_project([f"dep {url('A', sha_a)}"])

        self.install()

        for name in ("A", "B"):
            folder = self.deps() / name
            self.assertTrue((folder / "packageupper.txt").is_file())
            self.assertTrue((folder / f"include/o2/{name.lower()}/{name.lower()}.h").is_file())
            self.assertTrue((folder / f"src/o2/{name.lower()}/{name.lower()}.cpp").is_file())
            self.assertTrue((folder / f"externals/{name}Ext/lib/{name.lower()}.lib").is_file())
            self.assertFalse((folder / "main.cpp").exists())
            self.assertFalse((folder / ".git").exists())

    def test_same_commit_reached_twice_is_fetched_once(self) -> None:
        sha_c = self.library("C")
        sha_a = self.library("A", [f"dep {url('C', sha_c)}"])
        sha_b = self.library("B", [f"dep {url('C', sha_c)}"])
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])

        self.install()

        fetches = [m for m in self.messages if m.startswith(f"Fetching {OWNER}/C@")]
        self.assertEqual(len(fetches), 1)
        self.assertTrue((self.deps() / "C/include").is_dir())

    def test_repository_names_ignore_case(self) -> None:
        sha_c = self.library("C")
        sha_a = self.library("A", [f"dep https://github.com/TESTER/c/tree/{sha_c}"])
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('C', sha_c)}"])

        self.install()

        self.assertEqual(self.dep_folders(), ["A", "C"])

    def test_commit_conflict_lists_every_path_and_leaves_deps_alone(self) -> None:
        sha_c1 = self.library("C")
        sha_c2 = self.commit("C", {"C/include/o2/c/extra.h": "// new\n"})
        sha_a = self.library("A", [f"dep {url('C', sha_c1)}"])
        sha_b = self.library("B", [f"dep {url('C', sha_c2)}"])
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        before = self.snapshot()

        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        message = self.assert_error(
            f"tester/c@{sha_c1[:7]} via App -> A (tester/A@{sha_a[:7]}/packageupper.txt:4)",
            f"tester/c@{sha_c2[:7]} via App -> B (tester/B@{sha_b[:7]}/packageupper.txt:4)",
        )
        self.assertIn("different commits", message)
        self.assertEqual(self.snapshot(), before)

    def test_failed_fetch_leaves_deps_alone(self) -> None:
        sha_a = self.library("A")
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        before = self.snapshot()

        missing = "0123456789abcdef0123456789abcdef01234567"
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('Missing', missing)}"])
        self.assert_error("could not fetch tester/Missing at commit 0123456")
        self.assertEqual(self.snapshot(), before)

    def test_missing_export_is_reported_with_its_line(self) -> None:
        sha_a = self.commit("A", {"packageupper.txt": "# exports\ninclude A/include\n"})
        self.set_project([f"dep {url('A', sha_a)}"])

        self.assert_error(f"tester/A@{sha_a[:7]}/packageupper.txt:2:", "does not exist")
        self.assertFalse(self.deps().exists())

    def test_dependency_without_manifest_is_an_error(self) -> None:
        sha_a = self.commit("A", {"readme.txt": "no manifest\n"})
        self.set_project(["", f"dep {url('A', sha_a)}"])

        self.assert_error("packageupper.txt:2:", "has no packageupper.txt")

    def test_dependency_removed_from_the_tree_is_deleted(self) -> None:
        sha_a = self.library("A")
        sha_b = self.library("B")
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        self.install()

        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()

        self.assertEqual(self.dep_folders(), ["A"])

    def test_install_replaces_edited_files_of_normal_dependencies(self) -> None:
        sha_a = self.library("A")
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        header = self.deps() / "A/include/o2/a/a.h"
        header.write_text("// edited\n", encoding="utf-8")
        (self.deps() / "A/stray.txt").write_text("stray\n", encoding="utf-8")

        self.install()

        self.assertEqual(header.read_text(encoding="utf-8"), "// A header\n")
        self.assertFalse((self.deps() / "A/stray.txt").exists())

    def test_local_dependency_survives_install_and_clean(self) -> None:
        sha_b = self.library("B")
        sha_a = self.library("A", [f"dep {url('B', sha_b)}"])
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        header = self.deps() / "A/include/o2/a/a.h"
        header.write_text("// edited locally\n", encoding="utf-8")

        self.set_project([f"local {url('A', sha_a)}"])
        self.install()
        self.assertEqual(header.read_text(encoding="utf-8"), "// edited locally\n")
        self.assertTrue((self.deps() / "B/include").is_dir())

        self.clean()
        self.assertEqual(header.read_text(encoding="utf-8"), "// edited locally\n")
        self.assertFalse((self.deps() / "B").exists())

    def test_local_dependency_uses_its_edited_manifest(self) -> None:
        sha_b = self.library("B")
        sha_a = self.library("A")
        self.set_project([f"local {url('A', sha_a)}"])
        self.install()
        self.assertFalse((self.deps() / "B").exists())

        manifest = self.deps() / "A/packageupper.txt"
        manifest.write_text(manifest.read_text() + f"dep {url('B', sha_b)}\n", encoding="utf-8")
        self.install()

        self.assertTrue((self.deps() / "B/include").is_dir())

    def test_local_dependency_whose_commit_conflicts_is_an_error(self) -> None:
        sha_a1 = self.library("A")
        sha_a2 = self.commit("A", {"A/include/o2/a/new.h": "// new\n"})
        sha_b = self.library("B", [f"dep {url('A', sha_a2)}"])
        self.set_project([f"local {url('A', sha_a1)}"])
        self.install()

        self.set_project([f"local {url('A', sha_a1)}", f"dep {url('B', sha_b)}"])
        self.assert_error(
            f"tester/a@{sha_a1[:7]} via App (packageupper.txt:1)",
            f"tester/a@{sha_a2[:7]} via App -> B",
        )

    def test_local_line_in_a_dependency_counts_as_dep(self) -> None:
        sha_b = self.library("B")
        sha_a = self.library("A", [f"local {url('B', sha_b)}"])
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        header = self.deps() / "B/include/o2/b/b.h"
        header.write_text("// edited\n", encoding="utf-8")

        self.install()

        self.assertEqual(header.read_text(encoding="utf-8"), "// B header\n")

    def test_clean_without_deps_folder_does_nothing(self) -> None:
        self.set_project([])
        self.clean()
        self.assertFalse(self.deps().exists())

    def test_two_owners_with_the_same_repository_name_is_an_error(self) -> None:
        sha_a = self.library("A")
        self.set_project([
            f"dep {url('A', sha_a)}",
            f"dep https://github.com/someone/A/tree/{sha_a}",
        ])
        self.assert_error("packageupper.txt:2:", "deps/A")

    # deps.props --------------------------------------------------------------

    def test_props_lists_include_folders_sources_and_headers(self) -> None:
        sha_b = self.library("B")
        sha_a = self.commit("A", {
            "packageupper.txt": "include A/include\nsrc A/src\nexternal ext/Lib\nexternal ext/NoHeaders\n",
            "A/include/o2/a/a.h": "",
            "A/include/o2/a/notes.txt": "",
            "A/src/o2/a/a.cpp": "",
            "A/src/o2/a/internal.hpp": "",
            "A/src/o2/a/legacy.c": "",
            "ext/Lib/include/lib.h": "",
            "ext/Lib/lib/lib.lib": "",
            "ext/NoHeaders/lib/x.lib": "",
            "packageupper.txt.extra": "",
        })
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        self.install()

        props = self.props()
        xml.dom.minidom.parseString(props)
        here = "$(MSBuildThisFileDirectory)"
        self.assertIn(
            "<AdditionalIncludeDirectories>"
            f"{here}A\\include;{here}A\\externals\\Lib\\include;"
            f"{here}B\\include;"
            "%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>",
            props,
        )
        self.assertIn(f'<ClCompile Include="{here}A\\src\\o2\\a\\a.cpp">', props)
        self.assertIn("<ObjectFileName>$(IntDir)deps\\A\\src\\o2\\a\\</ObjectFileName>", props)
        self.assertIn(f'<ClCompile Include="{here}A\\src\\o2\\a\\legacy.c">', props)
        self.assertIn(f'<ClCompile Include="{here}B\\src\\o2\\b\\b.cpp">', props)
        self.assertIn(f'<ClInclude Include="{here}A\\include\\o2\\a\\a.h" />', props)
        self.assertIn(f'<ClInclude Include="{here}A\\src\\o2\\a\\internal.hpp" />', props)
        self.assertNotIn("notes.txt", props)
        self.assertNotIn("NoHeaders", props)
        self.assertNotIn("lib.lib", props)

    def test_props_escapes_special_characters(self) -> None:
        sha_a = self.commit("A", {
            "packageupper.txt": "src A/src\n",
            "A/src/R&D 100%;$x@.cpp": "",
        })
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()

        props = self.props()
        xml.dom.minidom.parseString(props)
        self.assertIn("R&amp;D 100%25%3B%24x%40.cpp", props)

    def test_props_follows_new_files_in_a_local_dependency(self) -> None:
        sha_a = self.library("A")
        self.set_project([f"local {url('A', sha_a)}"])
        self.install()
        self.assertNotIn("added.cpp", self.props())

        (self.deps() / "A/src/o2/a/added.cpp").write_text("", encoding="utf-8")
        self.install()

        self.assertIn("added.cpp", self.props())

    def test_clean_rewrites_props_with_only_local_dependencies(self) -> None:
        sha_a = self.library("A")
        sha_b = self.library("B")
        self.set_project([f"local {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        self.install()
        self.assertIn("b.cpp", self.props())

        self.clean()

        props = self.props()
        self.assertIn("a.cpp", props)
        self.assertNotIn("b.cpp", props)

    def test_props_is_written_when_there_are_no_dependencies(self) -> None:
        self.set_project([])
        self.install()

        props = self.props()
        xml.dom.minidom.parseString(props)
        self.assertIn("<AdditionalIncludeDirectories>%(AdditionalIncludeDirectories)", props)

    def native_library(self, name: str, extra_lines: list, deps: list = None) -> str:
        """Commits a library whose external has .lib and .dll files for x64 and Win32."""
        lines = [f"src {name}/src", f"external externals/{name}Ext"] + extra_lines + (deps or [])
        return self.commit(name, {
            "packageupper.txt": "\n".join(lines) + "\n",
            f"{name}/src/{name.lower()}.cpp": "",
            f"externals/{name}Ext/lib/x64/{name.lower()}.lib": "",
            f"externals/{name}Ext/lib/x64/{name.lower()}.dll": "",
            f"externals/{name}Ext/lib/x86/{name.lower()}.lib": "",
            f"externals/{name}Ext/lib/x86/{name.lower()}.dll": "",
            f"externals/{name}Ext/lib/shared.dll": "",
        })

    def test_props_links_libraries_and_copies_dlls_per_platform(self) -> None:
        sha_b = self.native_library("B", [
            "link externals/BExt/lib/x64/b.lib x64",
            "link externals/BExt/lib/x86/b.lib Win32",
            "copy externals/BExt/lib/x64/b.dll x64",
            "copy externals/BExt/lib/x86/b.dll Win32",
            "syslib winmm.lib",
        ])
        sha_a = self.native_library("A", ["syslib d3d11.lib x64"], [f"dep {url('B', sha_b)}"])
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()

        props = self.props()
        xml.dom.minidom.parseString(props)
        here = "$(MSBuildThisFileDirectory)"
        self.assertIn(
            "  <ItemDefinitionGroup>\n    <Link>\n"
            "      <AdditionalDependencies>winmm.lib;%(AdditionalDependencies)</AdditionalDependencies>",
            props,
        )
        self.assertIn(
            "  <ItemDefinitionGroup Condition=\"'$(Platform)'=='x64'\">\n    <Link>\n"
            f"      <AdditionalDependencies>d3d11.lib;{here}B\\externals\\BExt\\lib\\x64\\b.lib;"
            "%(AdditionalDependencies)</AdditionalDependencies>",
            props,
        )
        self.assertIn(
            "  <ItemDefinitionGroup Condition=\"'$(Platform)'=='Win32'\">\n    <Link>\n"
            f"      <AdditionalDependencies>{here}B\\externals\\BExt\\lib\\x86\\b.lib;"
            "%(AdditionalDependencies)</AdditionalDependencies>",
            props,
        )
        self.assertIn(
            "  <ItemGroup Condition=\"'$(Platform)'=='x64'\">\n"
            f'    <PackageUpperCopy Include="{here}B\\externals\\BExt\\lib\\x64\\b.dll" />',
            props,
        )
        self.assertIn(
            '<Copy SourceFiles="@(PackageUpperCopy)" DestinationFolder="$(OutDir)" SkipUnchangedFiles="true" />',
            props,
        )

    def test_missing_link_file_is_an_error_and_leaves_deps_alone(self) -> None:
        sha_a = self.native_library("A", [])
        self.set_project([f"dep {url('A', sha_a)}"])
        self.install()
        before = self.snapshot()

        sha_b = self.native_library("B", ["", "link externals/BExt/lib/x64/missing.lib x64"])
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        self.assert_error(f"tester/B@{sha_b[:7]}/packageupper.txt:4:", "missing.lib\" does not exist")
        self.assertEqual(self.snapshot(), before)

    def test_two_dependencies_copying_the_same_dll_is_an_error(self) -> None:
        sha_a = self.native_library("A", ["copy externals/AExt/lib/shared.dll"])
        sha_b = self.native_library("B", ["copy externals/BExt/lib/shared.dll x64"])
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])

        self.assert_error(
            f"tester/B@{sha_b[:7]}/packageupper.txt:3:", "shared.dll", f"tester/A@{sha_a[:7]}/packageupper.txt:3"
        )

    def test_same_dll_name_on_different_platforms_is_fine(self) -> None:
        sha_a = self.native_library("A", ["copy externals/AExt/lib/shared.dll x64"])
        sha_b = self.native_library("B", ["copy externals/BExt/lib/shared.dll Win32"])
        self.set_project([f"dep {url('A', sha_a)}", f"dep {url('B', sha_b)}"])

        self.install()

        self.assertEqual(self.props().count("<PackageUpperCopy "), 2)

    def test_clean_keeps_link_lines_of_local_dependencies(self) -> None:
        sha_a = self.native_library("A", ["link externals/AExt/lib/x64/a.lib x64"])
        sha_b = self.native_library("B", ["link externals/BExt/lib/x64/b.lib x64"])
        self.set_project([f"local {url('A', sha_a)}", f"dep {url('B', sha_b)}"])
        self.install()

        self.clean()

        props = self.props()
        self.assertIn("a.lib", props)
        self.assertNotIn("b.lib", props)


class ParseTest(unittest.TestCase):

    def parse_error(self, text: str) -> str:
        with self.assertRaises(packageupper.UserError) as context:
            packageupper.parse_manifest(text, "packageupper.txt")
        return str(context.exception)

    def test_parses_entries_and_skips_comments(self) -> None:
        sha = "a" * 40
        manifest = packageupper.parse_manifest(
            "# comment\n\ninclude  Lib/include\nsrc Lib\\src\n"
            f"dep https://github.com/Owner/Repo/tree/{sha.upper()}/\n",
            "packageupper.txt",
        )
        self.assertEqual([(e.kind, e.path, e.line) for e in manifest.exports],
                         [("include", "Lib/include", 3), ("src", "Lib/src", 4)])
        dep = manifest.deps[0]
        self.assertEqual((dep.kind, dep.owner, dep.repo, dep.sha, dep.line),
                         ("dep", "Owner", "Repo", sha, 5))

    def test_branch_name_is_an_error(self) -> None:
        message = self.parse_error("\ndep https://github.com/o/r/tree/main\n")
        self.assertIn("packageupper.txt:2:", message)
        self.assertIn("not a full commit SHA", message)

    def test_short_sha_is_an_error(self) -> None:
        message = self.parse_error("dep https://github.com/o/r/tree/9cdc757\n")
        self.assertIn("packageupper.txt:1:", message)

    def test_non_github_url_is_an_error(self) -> None:
        message = self.parse_error("dep https://gitlab.com/o/r/tree/" + "a" * 40 + "\n")
        self.assertIn("not a GitHub tree URL", message)

    def test_unknown_entry_is_an_error(self) -> None:
        message = self.parse_error("include a\nexport b\n")
        self.assertIn("packageupper.txt:2: unknown entry \"export\"", message)

    def test_parent_paths_are_an_error(self) -> None:
        self.assertIn("packageupper.txt:1:", self.parse_error("include ../secrets\n"))
        self.assertIn("packageupper.txt:1:", self.parse_error("include /etc\n"))
        self.assertIn("packageupper.txt:1:", self.parse_error("include C:/Windows\n"))

    def test_exports_with_the_same_folder_name_are_an_error(self) -> None:
        message = self.parse_error("include A/include\nsrc B/include\n")
        self.assertIn("packageupper.txt:2:", message)
        self.assertIn("line 1", message)

    def test_external_goes_under_externals(self) -> None:
        manifest = packageupper.parse_manifest(
            "include A/include\nexternal ext/include\n", "packageupper.txt"
        )
        targets = [packageupper.export_target(e.kind, e.path) for e in manifest.exports]
        self.assertEqual(targets, ["include", "externals/include"])

    def test_parses_link_copy_and_syslib(self) -> None:
        manifest = packageupper.parse_manifest(
            "link ext/lib/x64/a.lib x64\ncopy ext/lib/a.dll\nsyslib winmm.lib Win32\nexternal ext\n",
            "packageupper.txt",
        )
        self.assertEqual(
            [(e.kind, e.value, e.platform, e.line) for e in manifest.build],
            [("link", "ext/lib/x64/a.lib", "x64", 1), ("copy", "ext/lib/a.dll", "", 2),
             ("syslib", "winmm.lib", "Win32", 3)],
        )
        self.assertEqual(packageupper.path_in_deps(manifest, "ext/lib/a.dll"), "externals/ext/lib/a.dll")

    def test_x86_platform_suggests_win32(self) -> None:
        message = self.parse_error("external ext\nlink ext/a.lib x86\n")
        self.assertIn("packageupper.txt:2: unknown platform \"x86\"", message)
        self.assertIn("Use Win32", message)

    def test_link_outside_an_external_is_an_error(self) -> None:
        message = self.parse_error("external ext\ninclude inc\nlink inc/a.lib\n")
        self.assertIn("packageupper.txt:3:", message)
        self.assertIn("not inside an exported external", message)

    def test_link_and_copy_need_the_right_file_type(self) -> None:
        self.assertIn("takes a .lib", self.parse_error("external ext\nlink ext/a.dll\n"))
        self.assertIn("takes a .dll", self.parse_error("external ext\ncopy ext/font.png\n"))

    def test_syslib_with_a_path_is_an_error(self) -> None:
        message = self.parse_error("syslib ext/winmm.lib\n")
        self.assertIn("packageupper.txt:1:", message)
        self.assertIn("Use \"link\"", message)

    def test_paths_with_spaces_are_an_error_for_build_lines(self) -> None:
        message = self.parse_error("external ext\nlink ext/my lib.lib x64\n")
        self.assertIn("packageupper.txt:2:", message)

    def test_missing_value_is_an_error(self) -> None:
        self.assertIn("packageupper.txt:1:", self.parse_error("dep\n"))


class MainTest(unittest.TestCase):

    def test_reports_errors_without_a_traceback(self) -> None:
        temp = Path(tempfile.mkdtemp(prefix="packageupper-test-"))
        try:
            script = Path(packageupper.__file__)
            result = subprocess.run(
                [sys.executable, str(script), "install", "--project", str(temp)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("error: packageupper.txt: file not found", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        finally:
            shutil.rmtree(temp)


if __name__ == "__main__":
    unittest.main()
