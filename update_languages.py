import ast
import itertools
import os
import sys
import tarfile
import urllib.parse
from typing import List

import spack.fetch_strategy
import spack.mirror
import spack.package_base
import spack.repo
import spack.spec
import spack.stage
import spack.util.crypto
import spack.util.executable

C_EXT = {".c"}
CXX_EXT = {".cpp", ".cc", ".cxx", ".c++"}
FORTRAN_EXT = {".f", ".f77", ".f90", ".f95", ".f03", ".f08"}

DOWNLOADED_DIGESTS = set()
DIGEST_TO_LANGS = {}
BATCH_SIZE = 100
DOWNLOAD_DIR = "downloads"

CURL = spack.util.executable.Executable("curl")
CURL.add_default_arg("-Lk", "--max-time", "60", "--parallel")


def iter_tarfile(p):
    try:
        tar = tarfile.open(p)
    except tarfile.ReadError:
        return False

    try:
        while tar.next() is not None:
            pass
    except Exception:
        pass

    for member in tar.members:
        member: tarfile.TarInfo
        if not member.isreg():
            continue

        yield member.name


def iter_zipfile(p):
    import re

    # look for local file headers
    with open(p, "rb") as f:
        contents = f.read()

    if not contents.startswith(b"PK"):
        return False

    for entry in re.finditer(b"\x50\x4b\x03\x04", contents):
        start = entry.start()
        end = start + 30
        header = contents[start:end]
        if len(header) != 30:
            continue

        path_len = int.from_bytes(header[26:28], "little")

        if path_len < 1 or end + path_len > len(contents):
            continue

        try:
            yield contents[end : end + path_len].decode("utf-8")
        except UnicodeDecodeError:
            continue


class LocateDependsOnStatement(ast.NodeVisitor):
    def __init__(self, pkgclass: str) -> None:
        super().__init__()
        self.pkgclass = pkgclass
        self.in_pkg_class = False
        self.stack: List[ast.stmt] = []
        self.depth = 0
        self.last_version_stack: List[ast.stmt] = []

    def generic_visit(self, node: ast.AST) -> None:
        if not self.in_pkg_class:
            if isinstance(node, ast.ClassDef):
                if node.name == self.pkgclass:
                    self.in_pkg_class = True

            super().generic_visit(node)
            return

        else:
            # look for version(...) function call
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "version":
                    self.last_version_stack[:] = self.stack
            elif isinstance(node, ast.FunctionDef):
                pass
            else:
                self.stack.append(node)
                super().generic_visit(node)
                self.stack.pop()


def run(packages):

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    for batch in itertools.batched((p for p in packages if p.has_code and p.versions), BATCH_SIZE):

        for f in os.scandir(DOWNLOAD_DIR):
            os.unlink(f.path)

        curl_args = []
        for pkg_cls in batch:
            v = spack.package_base.preferred_version(pkg_cls)

            pkg = pkg_cls(spack.spec.Spec(f"{pkg_cls.name}@={v}"))

            try:
                stage: spack.stage.Stage = pkg.stage[0]
            except Exception as e:
                print(f"Skipping {pkg_cls.name}: {e}", file=sys.stderr)
                continue

            if not isinstance(stage.fetcher, spack.fetch_strategy.URLFetchStrategy):
                print(f"Skipping {pkg_cls.name}: no url fetcher", file=sys.stderr)
                continue

            url = stage.fetcher.url

            if urllib.parse.urlparse(url).scheme == "file":
                print(f"Skipping {pkg_cls.name}: file url", file=sys.stderr)
                continue

            digest = stage.fetcher.digest

            if digest is None:
                print(f"Skipping {pkg_cls.name}: no digest", file=sys.stderr)
                continue

            if digest in DOWNLOADED_DIGESTS:
                continue

            DOWNLOADED_DIGESTS.add(digest)

            curl_args.extend((url, "-o", os.path.join(DOWNLOAD_DIR, digest)))

        # Download the batch
        CURL(*curl_args, fail_on_error=False)

        # Figure out languages
        for archive in os.scandir(DOWNLOAD_DIR):
            c, cxx, fortran = False, False, False

            iter = iter_tarfile(archive.path)
            if iter is False:
                iter = iter_zipfile(archive.path)
            if iter is False:
                continue

            for path in iter:
                print(f"{archive.path}:{path}")
                _, ext = os.path.splitext(path)
                ext = ext.lower()
                if ext in C_EXT:
                    c = True
                elif ext in CXX_EXT:
                    cxx = True
                elif ext in FORTRAN_EXT:
                    fortran = True

            languages = []
            if c:
                languages.append("c")
            if cxx:
                languages.append("cxx")
            if fortran:
                languages.append("fortran")

            if not languages:
                continue

            DIGEST_TO_LANGS[archive.name] = languages

        # Update packages
        for pkg_cls in batch:
            v = spack.package_base.preferred_version(pkg_cls)
            version_info = pkg_cls.versions[v]

            digest_attrs = list(spack.util.crypto.hashes.keys()) + ["checksum"]
            digest_attr = next((x for x in digest_attrs if x in version_info), None)

            if digest_attr is None:
                continue

            digest = version_info[digest_attr]
            languages = DIGEST_TO_LANGS.get(digest, None)

            if languages is None:
                continue

            pkg_file = spack.repo.PATH.filename_for_package_name(pkg_cls.name)

            with open(pkg_file, "r+") as f:
                contents = f.read()
                tree = ast.parse(contents)

                visitor = LocateDependsOnStatement(pkg_cls.__name__)
                visitor.visit(tree)

                if not visitor.last_version_stack:
                    print(f"Skipped {pkg_file}")
                    continue

                location = visitor.last_version_stack[0].end_lineno

                lines = contents.split("\n")

                langauge_statements = [
                    "",
                    *(
                        f'    depends_on("{lang}", type="build")  # generated'
                        for lang in languages
                    ),
                ]

                lines = lines[:location] + langauge_statements + lines[location:]

                f.seek(0)
                f.write("\n".join(lines))
                f.truncate()


if __name__ == "__main__":
    start_at = sys.argv[1] if len(sys.argv) > 1 else None
    pkgs = spack.repo.PATH.all_package_classes()
    if start_at:
        pkgs = itertools.dropwhile(lambda x: x.name != start_at, pkgs)
    run(pkgs)
