#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile
import argparse
import contextlib
import hashlib
import io
import json
import pathlib
import infparser
import sqlite3
import sys
from xml.etree import ElementTree
from datetime import date, datetime


class DriverVersion:
    """
    Version of a DriverVer directive: w.x.y.z, each part 0 to 65534 (validated by the parser).
    https://learn.microsoft.com/windows-hardware/drivers/install/inf-driverver-directive
    """
    release: tuple[int, ...]

    def __init__(self, text: str):
        self.release = tuple(int(part) for part in text.split('.'))

    def __str__(self) -> str:
        return '.'.join(str(part) for part in self.release)

    def __repr__(self) -> str:
        return f"DriverVersion('{self}')"

    def __eq__(self, other) -> bool:
        return isinstance(other, DriverVersion) and self.release == other.release

    def __hash__(self) -> int:
        return hash(self.release)


ARCHITECTURES = ("x86", "ia64", "amd64", "arm", "arm64")
# https://learn.microsoft.com/windows-hardware/drivers/install/inf-ddinstall-section
DDINSTALL_SUBSECTIONS = ("coinstallers", "services", "interfaces", "hw", "events", "components", "software", "wmi", "factdef", "logconfigoverride", "filters")


def parseTargetOSVersion(decoration: str) -> tuple[str, int | None, int | None, str | None, str | None, int | None] | None:
    """
    Parses a TargetOSVersion decoration into (arch, osMajor, osMinor, productType, suiteMask, build).
    https://learn.microsoft.com/windows-hardware/drivers/install/inf-manufacturer-section
    NT[Architecture][.[OSMajorVersion][.[OSMinorVersion][.[ProductType][.[SuiteMask][.[BuildNumber]]]]]]
    Every field is optional; a missing architecture (NT) means x86 since Windows Server 2003 SP1.
    Returns None for an invalid decoration.
    """
    parts = decoration.strip().split('.')
    head = parts[0].lower()
    if not head.startswith("nt") or len(parts) > 6:
        return None

    arch = head[2:] or "x86"
    if arch not in ARCHITECTURES:
        return None

    major, minor, productType, suiteMask, build = (parts[1:] + [''] * 5)[:5]
    for number in (major, minor, build):
        if number and not number.isdigit():
            return None
    for flags in (productType, suiteMask):
        if flags:
            try:
                int(flags, 0)
            except ValueError:
                return None

    return (arch, int(major) if major else None, int(minor) if minor else None,
            productType or None, suiteMask or None, int(build) if build else None)


class Manifest:
    """
    Files of a driver package that are not (yet) available locally, e.g. an upload of which only the
    INF files were transferred: paths relative to root with their sizes. DriverFile resolves files
    against the manifest instead of the file system.
    """
    root: pathlib.Path
    sizes: dict[pathlib.Path, int | None]
    # directory -> {lowercased name: name}
    listings: dict[pathlib.Path, dict[str, str]]

    def __init__(self, root: pathlib.Path, files: list[tuple[str, int | None]]):
        self.root = root.absolute()
        self.sizes = {}
        self.listings = {}
        for relative, size in files:
            parts = pathlib.PurePosixPath(relative).parts
            if not parts or ".." in parts or pathlib.PurePosixPath(relative).is_absolute():
                continue
            current = self.root
            for part in parts:
                self.listings.setdefault(current, {})[part.lower()] = part
                current = current.joinpath(part)
            self.sizes[current] = size

    @classmethod
    def load(cls, path: str, root: str | None = None) -> "Manifest":
        # JSON list of {"path": "relative/path", "size": 123}, root defaults to the manifest's directory
        entries = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return cls(pathlib.Path(root) if root else pathlib.Path(path).parent,
                   [(e["path"], e.get("size")) for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)])

    def listing(self, directory: pathlib.Path) -> dict[str, str]:
        return self.listings.get(directory.absolute(), {})

    def isFile(self, path: pathlib.Path) -> bool:
        return path.absolute() in self.sizes

    def relative(self, path: pathlib.Path) -> str:
        return path.absolute().relative_to(self.root).as_posix()


class SourceDisk:
    # [SourceDisksNames[.arch]]
    # diskid = disk-description[,[tag-or-cab-file],[unused],[path],[flags][,tag-file]]
    path: pathlib.PurePosixPath
    tagOrCabFile: str
    flags: int
    tagFile: str

    def __init__(self, path: pathlib.PurePosixPath, tagOrCabFile: str, flags: int, tagFile: str):
        self.path = path
        self.tagOrCabFile = tagOrCabFile
        self.flags = flags
        self.tagFile = tagFile

    def cabFile(self) -> str | None:
        # with flags 0x10 tag-or-cab-file is always the cabinet, otherwise only if it has a .cab extension
        if self.tagOrCabFile and (self.flags & 0x10 or self.tagOrCabFile.lower().endswith(".cab")):
            return self.tagOrCabFile
        return None

    def tagFileName(self) -> str | None:
        if self.flags & 0x10:
            return self.tagFile or None
        # a .cab file is used as both, tag and cabinet file
        return self.tagOrCabFile or None


class SourceFile:
    # [SourceDisksFiles[.arch]]
    # filename=diskid[,[subdir][,size]]
    name: str
    diskid: str
    subdir: pathlib.PurePosixPath

    def __init__(self, name: str, diskid: str, subdir: pathlib.PurePosixPath):
        self.name = name
        self.diskid = diskid
        self.subdir = subdir


def relativeInfPath(value: str) -> pathlib.PurePosixPath | None:
    """
    Converts an INF path (\\dir\\subdir, relative to the installation root) into a
    relative posix path. Returns None if the path leaves the installation root.
    """
    parts: list[str] = []
    for part in pathlib.PureWindowsPath(value.strip()).parts:
        if part in ('\\', '/') or part.endswith(':\\') or part.endswith(':'):
            continue
        if part == '..':
            if not parts:
                return None
            parts.pop()
        elif part != '.':
            parts.append(part)
    return pathlib.PurePosixPath(*parts)


class ModelsDecoration:
    """A Models section listed in [Manufacturer] (models-section-name[.TargetOSVersion])."""
    # undecorated Models section name as listed (section names are case-insensitive)
    section: str
    # TargetOSVersion as listed ('' for an undecorated entry)
    decoration: str
    arch: str
    osMajor: int | None
    osMinor: int | None
    productType: int | None
    suiteMask: int | None
    build: int | None

    def __init__(self, section: str, decoration: str, arch: str, osMajor: int | None = None, osMinor: int | None = None,
                 productType: str | None = None, suiteMask: str | None = None, build: int | None = None):
        self.section = section
        self.decoration = decoration
        self.arch = arch
        self.osMajor = osMajor
        self.osMinor = osMinor
        self.productType = int(productType, 0) if productType else None
        self.suiteMask = int(suiteMask, 0) if suiteMask else None
        self.build = build

    def sectionName(self) -> str:
        return f"{self.section}.{self.decoration}" if self.decoration else self.section


class DriverTarget:
    HardwareID: str
    # "hardware" for the hardware id of a models entry, "compatible" for compatible ids
    Kind: str
    DeviceDescription: str
    date: date | None
    version: DriverVersion | None
    Architecture: str | None
    OSMajorVersion: int | None
    OSMinorVersion: int | None
    ProductType: int | None
    SuiteMask: int | None
    BuildNumber: int | None
    # the DDInstall section (with its platform extension) installing this target
    InstallSection: str
    # the Models section listing this target
    Models: ModelsDecoration
    # files of the driver package required by this target (including the INF)
    files: set[pathlib.Path]

    def __init__(self, hardwareID: str, deviceDescription: str, date: date | None, version: DriverVersion | None, files: set[pathlib.Path]):
        self.HardwareID = hardwareID
        self.Kind = "hardware"
        self.DeviceDescription = deviceDescription
        self.date = date
        self.version = version
        self.Architecture = self.OSMajorVersion = self.OSMinorVersion = self.ProductType = self.SuiteMask = self.BuildNumber = None
        self.InstallSection = ""
        self.files = files


class DriverFile:
    valid: bool
    klass: str
    provider: str
    # sha256 of the INF file, identifies the driver package
    sha256: str
    infPath: pathlib.Path
    rootPath: pathlib.Path
    version: DriverVersion | None
    date: date | None
    targets: list[DriverTarget]
    # every Models section listed in [Manufacturer], also empty or missing ones (they exclude a target OS)
    models: list[ModelsDecoration]
    inf: infparser.InfFile
    sections: dict[str, list[tuple[str, str, infparser.Entry]]]
    # CatalogFile[.decoration] from [Version], keyed by decoration ('', 'nt', 'ntamd64', ...),
    # relative to the INF directory
    catalogFiles: dict[str, pathlib.PurePosixPath]
    sectionFiles: dict[str, set[str]]
    # (ddinstall, subsection) pairs already reported as ignored due to a decoration mismatch
    ignoredSubsections: set[tuple[str, str]]
    # tuple(arch, diskid), arch is '' for the undecorated section
    sourceDiskNames: dict[tuple[str, str], SourceDisk]
    # tuple(arch, lowercased filename), arch is '' for the undecorated section
    sourceDiskFiles: dict[tuple[str, str], SourceFile]
    # tuple(arch, lowercased filename) -> resolved files (the file itself or its cabinet, plus tag file)
    resolvedFiles: dict[tuple[str, str], set[pathlib.Path]]
    # directory -> {lowercased name: name}, for case-insensitive lookups
    directoryListings: dict[pathlib.Path, dict[str, str]]
    # resolved paths of all INFs of this scan (CopyINF recursion)
    visited: set[str]
    copyInfs: dict[str, "DriverFile | None"]
    # CopyINF value -> located INF (None if invalid or missing)
    copyInfPaths: dict[str, pathlib.Path | None]
    # files are not available locally (built-in drivers from a WIM image)
    wim: bool
    # files resolved against a manifest instead of the file system (e.g. an upload in progress)
    manifest: Manifest | None
    # files referenced by the INF that are not part of the driver package (INF directory + relative path)
    missing: set[pathlib.Path]
    # device ids dropped by ExcludeID: (id, kind, models decoration, install section)
    excluded: list[tuple[str, str, "ModelsDecoration", str]]
    # key to value mapping for string replacement (e.g. %strkey% in the model section)
    strings: dict[str, str]

    def __init__(self, path: str, data: bytes | None = None, visited: set[str] | None = None, manifest: Manifest | None = None):
        """
        path: the INF file; for built-in drivers of a WIM image its path inside the image, with its
        content passed as data (the files of the driver package are not available then)
        """
        self.valid = False
        self.klass = ""
        self.provider = ""
        self.infPath = pathlib.Path(path)
        self.rootPath = self.infPath.parent
        self.version = None
        self.date = None
        self.sections = {}
        self.catalogFiles = {}
        self.sectionFiles = {}
        self.ignoredSubsections = set()
        self.sourceDiskNames = {}
        self.sourceDiskFiles = {}
        self.resolvedFiles = {}
        self.directoryListings = {}
        self.wim = data is not None
        self.manifest = manifest
        self.missing = set()
        self.excluded = []
        self.visited = visited if visited is not None else set()
        self.visited.add(str(self.infPath.resolve()) if not self.wim else path)
        self.copyInfs = {}
        self.copyInfPaths = {}
        self.strings = {}
        self.targets = []
        self.models = []

        if data is None:
            data = pathlib.Path(path).read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.inf = infparser.InfFile.load(data, name=path)

        for warning in self.inf.warnings:
            print("warning: " + warning)

        # section names and keys are case-insensitive (the parser already
        # merged duplicate sections); lines without a key (file lists,
        # manufacturer names) keep their comma separated values as key
        for normalized, section in self.inf.sections.items():
            self.sections[normalized] = []
            for entry in section.entries:
                if entry.rawKey is not None:
                    key, value = entry.rawKey, ','.join(entry.rawValues)
                else:
                    key, value = ','.join(entry.rawValues), ''

                if not key and not value: continue
                # we normalize the key, but not the value which is normalized
                # within the used context
                self.sections[normalized].append((key.lower(), value, entry))

        if not "version" in self.sections:
            print("error: no version section found")
            return

        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-version-section
        # If signature-name is none of these string values, the file is not accepted as a valid INF
        signatures = [entry.values[0] for key, _, entry in self.sections["version"] if key == "signature" and entry.values]
        if not signatures or signatures[0].lower() not in ("$windows nt$", "$chicago$"):
            print(f"error: invalid signature {signatures[0] if signatures else '(missing)'}, expected \"$Windows NT$\" or \"$Chicago$\"")
            return

        self._parseVersion()
        self.valid = True


    def parseDevices(self):
        self._parseSourceFiles()
        self._parseStrings()
        self._parseManufacturer()



    def _sourceDisksArch(self, section: str) -> str | None:
        # the platform extensions of SourceDisksNames/SourceDisksFiles are .x86, .amd64, ...
        # and not .ntx86, .ntamd64, ... like for other sections
        arch = section.split('.', 1)[1] if '.' in section else ''
        if arch == '' or arch in ARCHITECTURES:
            return arch
        if arch.startswith("nt") and arch[2:] in ARCHITECTURES:
            print(f"warning: [{section}] is ignored, the platform extension must be .{arch[2:]} (not .{arch})")
        else:
            print(f"warning: [{section}] has an unknown platform extension")
        return None


    def _parseSourceFiles(self):
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-sourcedisksnames-section
        for section in self.inf.sectionNames():
            if section != "sourcedisksnames" and not section.startswith("sourcedisksnames."):
                continue
            arch = self._sourceDisksArch(section)
            if arch is None:
                continue

            for entry in self.inf.entries(section):
                # diskid = disk-description[,tag-or-cab-file] |
                # diskid = disk-description[,[tag-or-cab-file][,[unused][,path]]] |
                # diskid = disk-description[,[tag-or-cab-file],[unused],[path][,flags]] |
                # diskid = disk-description[,[tag-or-cab-file],[unused],[path],[flags][,tag-file]]  (Windows XP and later versions of Windows)
                diskid = (entry.key or "").strip()
                if not diskid.isdigit():
                    print(f"warning: invalid diskid {diskid!r} in [{section}] (line {entry.line})")
                    continue
                (_, tagOrCabFile, _, path, flags, tagFile) = (entry.values + [''] * 6)[:6]

                # path is relative to the installation root (the INF directory)
                diskPath = relativeInfPath(path)
                if diskPath is None:
                    print(f"error: path {path} of diskid {diskid} leaves the driver package (line {entry.line})")
                    continue

                try:
                    diskFlags = int(flags, 0) if flags else 0
                except ValueError:
                    print(f"warning: invalid flags {flags} for diskid {diskid} (line {entry.line})")
                    diskFlags = 0

                if (arch, diskid) in self.sourceDiskNames:
                    print(f"warning: diskid {diskid} defined multiple times in [{section}] (line {entry.line})")
                    continue
                self.sourceDiskNames[(arch, diskid)] = SourceDisk(diskPath, tagOrCabFile, diskFlags, tagFile)

        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-sourcedisksfiles-section
        for section in self.inf.sectionNames():
            if section != "sourcedisksfiles" and not section.startswith("sourcedisksfiles."):
                continue
            arch = self._sourceDisksArch(section)
            if arch is None:
                continue

            for entry in self.inf.entries(section):
                # filename=diskid[,[ subdir][,size]], the file name must be the exact name (no %strkey%)
                if entry.rawKey is None or not entry.values or not entry.values[0]:
                    print(f"warning: missing diskid in [{section}] (line {entry.line})")
                    continue
                filename = entry.rawKey
                subdir = relativeInfPath(entry.values[1]) if len(entry.values) > 1 else pathlib.PurePosixPath()
                if subdir is None:
                    print(f"error: subdir of {filename} leaves the driver package (line {entry.line})")
                    continue
                if (arch, filename.lower()) in self.sourceDiskFiles:
                    print(f"warning: {filename} listed multiple times in [{section}] (line {entry.line})")
                    continue
                self.sourceDiskFiles[(arch, filename.lower())] = SourceFile(filename, entry.values[0].strip(), subdir)


    def _locate(self, relative: pathlib.PurePosixPath) -> pathlib.Path | None:
        """Finds a file relative to the INF directory, ignoring the case like Windows does."""
        if self.wim:
            # files of built-in drivers are not available
            return self.rootPath.joinpath(relative)

        # always use the name from the directory listing, so the result is the actual
        # file name on case-sensitive and case-insensitive file systems alike
        current = self.rootPath
        for part in relative.parts:
            if self.manifest is not None:
                listing = self.manifest.listing(current)
            else:
                if current not in self.directoryListings:
                    try:
                        self.directoryListings[current] = {name.lower(): name for name in os.listdir(current)}
                    except OSError:
                        self.directoryListings[current] = {}
                listing = self.directoryListings[current]
            match = listing.get(part.lower())
            if match is None:
                return None
            current = current.joinpath(match)
        if self.manifest is not None:
            return current if self.manifest.isFile(current) else None
        return current if current.is_file() else None


    def _resolveSourceFile(self, name: str, arch: str) -> set[pathlib.Path]:
        """
        Maps a source file name of a CopyFiles directive to its location in the driver package:
        [SourceDisksFiles.arch] -> [SourceDisksFiles] for the file, [SourceDisksNames.arch] ->
        [SourceDisksNames] for its disk. Returns the file (or its cabinet) and the disk's tag file.
        """
        key = (arch, name.lower())
        if key in self.resolvedFiles:
            return self.resolvedFiles[key]

        resolved: set[pathlib.Path] = set()
        self.resolvedFiles[key] = resolved

        sourceFile = self.sourceDiskFiles.get(key) or self.sourceDiskFiles.get(('', name.lower()))
        disk = None
        if sourceFile is None:
            print(f"warning: {name} is not listed in [SourceDisksFiles] for {arch}, assuming it is next to the INF")
            relative = pathlib.PurePosixPath(name)
        else:
            disk = self.sourceDiskNames.get((arch, sourceFile.diskid)) or self.sourceDiskNames.get(('', sourceFile.diskid))
            if disk is None:
                print(f"warning: {name} uses diskid {sourceFile.diskid}, which is not listed in [SourceDisksNames] for {arch}")
                relative = sourceFile.subdir.joinpath(sourceFile.name)
            else:
                relative = disk.path.joinpath(sourceFile.subdir, sourceFile.name)

        path = self._locate(relative)
        if path is None and disk and disk.cabFile():
            # files that cannot be found on the media are extracted from the disk's cabinet file
            path = self._locate(disk.path.joinpath(disk.cabFile() or ""))
        if path is None:
            print(f"error: {relative} not found in the driver package ({arch})")
            self.missing.add(self.rootPath.joinpath(relative))
        else:
            resolved.add(path)

        # tag files are optional for fixed media, include them if present
        if disk and disk.tagFileName():
            tag = self._locate(disk.path.joinpath(disk.tagFileName() or ""))
            if tag:
                resolved.add(tag)

        return resolved


    def _copyInfFiles(self, value: str, arch: str) -> set[pathlib.Path]:
        """
        https://learn.microsoft.com/windows-hardware/drivers/install/inf-copyinf-directive
        The INF must reside in the directory of this INF or a subdirectory. Starting with
        Windows Vista the complete driver package of the INF is copied to the driver store
        (recursively), so all of its files for the architecture are required as well.
        """
        if value.lower() in self.copyInfPaths:
            path = self.copyInfPaths[value.lower()]
        else:
            relative = relativeInfPath(value)
            path = None
            if relative is None or not relative.parts:
                print(f"error: invalid CopyINF {value}, the INF must be in the driver package")
            else:
                path = self._locate(relative)
                if path is None:
                    print(f"error: CopyINF {relative} not found in the driver package")
                    self.missing.add(self.rootPath.joinpath(relative))
            self.copyInfPaths[value.lower()] = path

        if path is None:
            return set()
        if self.wim:
            return {path}

        key = str(path.resolve())
        if key not in self.copyInfs:
            if key in self.visited:
                # already part of this scan (e.g. INFs referencing each other)
                self.copyInfs[key] = None
            else:
                child = DriverFile(str(path), visited=self.visited, manifest=self.manifest)
                if child.valid:
                    child.parseDevices()
                    self.missing.update(child.missing)
                self.copyInfs[key] = child if child.valid else None

        files = {path}
        child = self.copyInfs[key]
        if child:
            for t in child.targets:
                if t.Architecture == arch:
                    files.update(t.files)
        return files


    def _parseStrings(self):
        # the parser selects the Strings section and unquotes its values
        self.strings = self.inf.strings


    def _fileSections(self, ddinstall: str, installSection: str, arch: str) -> set[str]:
        """
        Sections of a device install that may contain CopyFiles directives:
        https://learn.microsoft.com/windows-hardware/drivers/install/inf-copyfiles-directive
        DDInstall, DDInstall.CoInstallers, add-interface sections (AddInterface in
        DDInstall.Interfaces), install-interface sections of [InterfaceInstall32]
        and ClassInstall32[.nt<arch>|.nt].
        """
        sections = {ddinstall}

        # all related DDInstall sections (.Services, .HW, .Interfaces, ...) must use the
        # same platform extension as the chosen DDInstall section, others are not processed
        # https://learn.microsoft.com/windows-hardware/drivers/install/combining-platform-extensions-with-other-section-name-extensions
        decorations = ["", ".nt"] + [f".nt{a}" for a in ARCHITECTURES]
        chosen = ddinstall[len(installSection):]
        for sub in DDINSTALL_SUBSECTIONS:
            if f"{ddinstall}.{sub}" in self.sections:
                continue
            others = [f"{installSection}{d}.{sub}" for d in decorations if d != chosen and f"{installSection}{d}.{sub}" in self.sections]
            if others and (ddinstall, sub) not in self.ignoredSubsections:
                self.ignoredSubsections.add((ddinstall, sub))
                print(f"warning: [{others[0]}] is not used for [{ddinstall}], expected [{ddinstall}.{sub}]")

        if f"{ddinstall}.coinstallers" in self.sections:
            sections.add(f"{ddinstall}.coinstallers")

        # AddInterface={InterfaceClassGUID}[,[reference-string][,[add-interface-section][,flags]]]
        for key, _, entry in self.sections.get(f"{ddinstall}.interfaces", []):
            if key == "addinterface" and len(entry.values) > 2 and entry.values[2]:
                sections.add(entry.values[2].lower())

        # {InterfaceClassGUID}=install-interface-section[,flags]
        for entry in self.inf.entries("interfaceinstall32"):
            if entry.values and entry.values[0]:
                sections.add(entry.values[0].lower())

        classInstall = self.inf.resolveDecorated("classinstall32", arch)
        if classInstall:
            sections.add(classInstall)

        return sections


    def _getSectionFiles(self, name: str, stack: list[str] = list()) -> set[str]:
        name = name.lower()
        if name in self.sectionFiles:
            return self.sectionFiles[name]

        if name not in self.sections:
            print(f"warning: section {name} not found")
            self.sectionFiles[name] = set()
            return set()
        elif name in stack:
            print(f"warning: recursion at {name} (start: {stack[0]})")
            self.sectionFiles[name] = set()
            return set()

        stack.append(name)
        files: set[str] = set()
        for key, _, entry in self.sections[name]:
            # ignoring "include":
            # this is for system supplied INF files which therefore are fully
            # included in the system already
            # 
            # ignoring "needs":
            # this is for sections in system supplied INF files which are
            # means all the files are already present in the system
            if key != "copyfiles":
                continue

            # CopyFiles=@filename | file-list-section[,file-list-section]...
            # file names must be exact names on the source media (no %strkey%)
            for value in entry.rawValues:
                if not value:
                    continue
                if value.startswith('@'):
                    files.add(value[1:].strip())
                    continue

                fileList = self.inf.expand(value, entry.line).lower()
                if fileList not in self.sections:
                    # sections may also be defined by system-supplied INFs listed in an Include
                    # directive, their files are part of Windows and not of this driver package
                    includes = [v for k, _, e in self.sections[name] if k == "include" for v in e.values if v]
                    if includes:
                        print(f"info: file list section [{fileList}] not found, assuming it is defined by {', '.join(includes)} (line {entry.line})")
                    else:
                        print(f"error: file list section [{fileList}] not found (line {entry.line})")
                    continue

                # destination-file-name[,source-file-name][,unused][,flag]
                # the source file name is the file in the driver package
                for fileEntry in self.inf.entries(fileList):
                    if not fileEntry.rawValues or not fileEntry.rawValues[0]:
                        continue
                    source = fileEntry.rawValues[1] if len(fileEntry.rawValues) > 1 and fileEntry.rawValues[1] else fileEntry.rawValues[0]
                    files.add(source)

        stack.remove(name)
        self.sectionFiles[name] = files
        return files


    def _parseModelSection(self, models: ModelsDecoration):
        modelSection = models.sectionName()
        arch = models.arch
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-models-section
        # A per-manufacturer Models section identifies at least one device,
        # references the DDInstall section of the INF file for that device,
        # and specifies a unique-to-the-model-section Hardware identifier (ID) for that device.
        if not self.inf.has(modelSection):
            # Windows does not fall back to a differently decorated (or undecorated) Models
            # section, so the INF does not apply to this target OS (like an empty section)
            print(f"warning: models section [{modelSection}] not found, the INF does not apply to this target OS")
            return

        # device-description=install-section-name[,hw-id][,compatible-id...]
        # The description belongs to the model entry, i.e. to the install section and
        # device id. The same id may be listed by several entries; Windows treats each
        # entry as a separate driver candidate, so every (id, install section, kind) is
        # its own target: an id can be the hardware id of one entry and a compatible id
        # of another entry of the same install section (like InfVerif lists them).
        deviceSections: dict[tuple[str, str, str], str] = {}
        for entry in self.inf.entries(modelSection):
            if entry.key is None or not entry.values or not entry.values[0]:
                print(f"warning: models entry without install section in [{modelSection}] (line {entry.line})")
                continue

            installSection = entry.values[0].lower()
            deviceIds = entry.values[1:]
            # INFs are required to specify at least one device ID for each entry
            if not any(deviceIds):
                print(f"warning: models entry without hardware or compatible id in [{modelSection}] (line {entry.line})")
                continue

            if self.inf.resolveDecorated(installSection, arch) is None:
                print(f"error: install section [{installSection}] for {arch} not found (line {entry.line})")
                continue

            # the parser already expanded %strkey% tokens (e.g. %DeviceDesc%)
            deviceDescription = entry.key
            for index, id in enumerate(deviceIds):
                id = id.upper() # devmgmt.msc always shows only upper cased hardware ids (?)
                if not id: continue
                # the first id is the hardware id, the following ones are compatible ids
                kind = "hardware" if index == 0 else "compatible"
                existing = deviceSections.get((id, installSection, kind))
                if existing is not None:
                    if existing != deviceDescription:
                        print(f"warning: {id} listed multiple times as {kind} id for {installSection} in [{modelSection}] (line {entry.line}), keeping the first description")
                    continue
                deviceSections[(id, installSection, kind)] = deviceDescription

        for (hardwareId, installSection, kind), name in deviceSections.items():
            # install-section-name.nt<arch> -> install-section-name.nt -> install-section-name
            ddinstall = self.inf.resolveDecorated(installSection, arch)
            if ddinstall is None:
                continue

            # ExcludeID: the DDInstall section does not install devices with these ids
            # (only processed when specified directly in the DDInstall section)
            excluded = {v.upper() for key, _, entry in self.sections[ddinstall] if key == "excludeid" for v in entry.values if v}
            if hardwareId in excluded:
                self.excluded.append((hardwareId, kind, models, ddinstall))
                continue

            sections: set[str] = {ddinstall}

            # DriverVer in a DDInstall section takes precedence over the one in [Version]
            targetDate, targetVersion = self.date, self.version
            driverVers = [entry for key, _, entry in self.sections[ddinstall] if key == "driverver"]
            if driverVers:
                targetDate, targetVersion = self._parseDriverVer(driverVers[0])

            sections.update(self._fileSections(ddinstall, installSection, arch))

            files: set[str] = set()
            for sectionName in sections:
                files = files.union(self._getSectionFiles(sectionName))

            mappedfiles: set[pathlib.Path] = {self.infPath}
            for f in sorted(files, key=str.lower):
                mappedfiles.update(self._resolveSourceFile(f, arch))

            # CopyINF=filename1.inf[,filename2.inf]... (DDInstall only)
            for key, _, entry in self.sections[ddinstall]:
                if key == "copyinf":
                    for value in entry.rawValues:
                        if value:
                            mappedfiles.update(self._copyInfFiles(value, arch))

            catalog = self._catalogFor(arch)
            if catalog:
                mappedfiles.add(catalog)

            device = DriverTarget(hardwareId, name, targetDate, targetVersion, mappedfiles)
            device.InstallSection = ddinstall
            device.Kind = kind
            device.Models = models
            device.Architecture = arch
            device.OSMajorVersion = models.osMajor
            device.OSMinorVersion = models.osMinor
            device.ProductType = models.productType
            device.SuiteMask = models.suiteMask
            device.BuildNumber = models.build
            self.targets.append(device)


    def _parseManufacturer(self):
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-manufacturer-section
        for entry in self.inf.entries("manufacturer"):
            # manufacturer-name |
            # %strkey%=models-section-name |
            # %strkey%=models-section-name [,TargetOSVersion] [,TargetOSVersion] ...  (Windows XP and later versions of Windows)
            if entry.key is None:
                # manufacturer-name: implicitly specifies the Models section of the same name
                if not entry.values or not entry.values[0]:
                    continue
                if len(entry.values) > 1:
                    print(f"warning: manufacturer-name entry {entry.values[0]} cannot have TargetOSVersion decorations (line {entry.line})")
                modelSection, decorations = entry.values[0], []
            else:
                if not entry.values or not entry.values[0]:
                    print(f"warning: missing models section name in [Manufacturer] (line {entry.line})")
                    continue
                modelSection = entry.values[0]
                decorations = [d for d in entry.values[1:] if d]

            listed: list[ModelsDecoration] = []
            if not decorations:
                # TODO: add support for DOS kernel handling
                # An undecorated Models section is only used on x86 since Windows Server 2003 SP1,
                # non-x86 platforms require the architecture decoration
                listed.append(ModelsDecoration(modelSection, "", "x86"))

            # Only the listed decorations are used: an undecorated Models section next to
            # decorated ones only applies to Windows versions before Windows XP
            for decoration in decorations:
                target = parseTargetOSVersion(decoration)
                if target is None:
                    print(f"warning: invalid TargetOSVersion {decoration} for models section {modelSection} (line {entry.line})")
                    continue
                listed.append(ModelsDecoration(modelSection, decoration, *target))

            for models in listed:
                if any(m.sectionName().lower() == models.sectionName().lower() for m in self.models):
                    continue
                self.models.append(models)
                self._parseModelSection(models)


    def _parseVersion(self):
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-version-section
        # LayoutFile is only used by system-supplied INFs (instead of SourceDisksNames/Files)
        driverVer = None
        for key, _, entry in self.sections["version"]:
            value = entry.values[0] if entry.values else ""
            if key == "class":
                self.klass = value
            elif key == "provider":
                self.provider = value
            elif key == "catalogfile" or key.startswith("catalogfile."):
                # CatalogFile[.nt|.ntx86|.ntamd64|...]=filename.cat, the catalog is
                # located next to the INF and is not listed in SourceDisksFiles
                decoration = key.split('.', 1)[1] if '.' in key else ''
                if not value:
                    print(f"warning: empty {entry.key} (line {entry.line})")
                    continue
                if '\\' in value or '/' in value:
                    print(f"warning: {entry.key} must be a file name without path, got {value} (line {entry.line})")
                if decoration in self.catalogFiles:
                    print(f"warning: {entry.key} defined multiple times (line {entry.line})")
                    continue
                self.catalogFiles[decoration] = pathlib.PurePosixPath(pathlib.PureWindowsPath(value).name)
            elif key == "driverver":
                if driverVer is not None:
                    print(f"warning: DriverVer defined multiple times (line {entry.line})")
                    continue
                driverVer = entry

        if driverVer is None:
            # Starting with Windows 2000, INF files must have a DriverVer directive;
            # without it Windows applies the default date of 00/00/0000
            print("warning: DriverVer missing in [Version]")
        else:
            self.date, self.version = self._parseDriverVer(driverVer)


    def _parseDriverVer(self, entry: infparser.Entry) -> tuple[date | None, DriverVersion | None]:
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-driverver-directive
        # DriverVer=mm/dd/yyyy[,w.x.y.z]; a hyphen can be used as the date separator,
        # an invalid date is treated as 00/00/0000
        dateValue = entry.values[0].strip() if entry.values else ""
        versionValue = entry.values[1].strip() if len(entry.values) > 1 else ""
        if len(entry.values) > 2:
            print(f"warning: DriverVer has extra values, expected mm/dd/yyyy,w.x.y.z (line {entry.line})")

        driverDate = None
        if dateValue and dateValue not in ("00/00/0000", "00-00-0000"):
            try:
                driverDate = datetime.strptime(dateValue.replace('-', '/'), "%m/%d/%Y").date()
            except ValueError:
                print(f"error: invalid DriverVer date {dateValue}, expected mm/dd/yyyy (line {entry.line})")

        driverVersion = None
        if versionValue:
            # each of w, x, y and z must be an integer >= 0 and < 65535, 0.0.0.0 is not valid
            parts = versionValue.split('.')
            if len(parts) > 4 or not all(p.isdigit() and int(p) < 65535 for p in parts):
                print(f"error: invalid DriverVer version {versionValue}, expected w.x.y.z (line {entry.line})")
            elif all(int(p) == 0 for p in parts):
                print(f"error: invalid DriverVer version {versionValue}, 0.0.0.0 is not valid (line {entry.line})")
            else:
                driverVersion = DriverVersion(versionValue)

        return driverDate, driverVersion


    @property
    def signed(self) -> bool:
        # without a CatalogFile entry the driver package is treated as unsigned
        return bool(self.catalogFiles)


    def _catalogFor(self, arch: str | None) -> pathlib.Path | None:
        # CatalogFile.nt<arch> -> CatalogFile.nt -> CatalogFile
        for decoration in ([f"nt{arch}"] if arch else []) + ["nt", ""]:
            if decoration in self.catalogFiles:
                catalog = self._locate(self.catalogFiles[decoration])
                if catalog is None and ("catalog", decoration) not in self.resolvedFiles:
                    self.resolvedFiles[("catalog", decoration)] = set()
                    print(f"error: catalog file {self.catalogFiles[decoration]} not found next to the INF")
                    self.missing.add(self.rootPath.joinpath(self.catalogFiles[decoration]))
                return catalog
        return None

SCHEMA_VERSION = 2
SCHEMA = """
    CREATE TABLE driver (
        id INTEGER PRIMARY KEY,
        -- sha256 of the INF: identifies the package, store folder is store/<sha256[:16]>
        sha256 TEXT NOT NULL,
        -- '' for stored packages, the WIM guid for built-in drivers (coverage only, no files)
        container TEXT NOT NULL DEFAULT '',
        -- package folder, relative to the database directory if it is inside of it
        root TEXT NOT NULL,
        -- INF path relative to root
        inf TEXT NOT NULL,
        class TEXT,
        provider TEXT,
        signed INTEGER NOT NULL,
        -- DriverVer of the [Version] section, date as YYYY-MM-DD
        date TEXT,
        version TEXT,
        UNIQUE (sha256, container)
    );

    -- WIM images scanned for built-in drivers, driver.container is their guid
    CREATE TABLE container (
        guid TEXT PRIMARY KEY,
        path TEXT,
        image INTEGER,
        arch TEXT,
        os_major INTEGER,
        os_minor INTEGER,
        os_build INTEGER,
        -- VER_NT_WORKSTATION (1), VER_NT_DOMAIN_CONTROLLER (2), VER_NT_SERVER (3)
        product_type INTEGER
    );

    -- every Models section listed in [Manufacturer], also empty or missing ones (they exclude a target OS)
    CREATE TABLE models (
        id INTEGER PRIMARY KEY,
        driver INTEGER NOT NULL REFERENCES driver(id) ON DELETE CASCADE,
        -- undecorated Models section name (lower case), one per manufacturer entry
        section TEXT NOT NULL,
        -- TargetOSVersion as listed ('' for an undecorated entry)
        decoration TEXT NOT NULL,
        arch TEXT NOT NULL,
        os_major INTEGER,
        os_minor INTEGER,
        os_product_type INTEGER,
        os_suite_mask INTEGER,
        os_build INTEGER
    );

    CREATE TABLE fileset (
        id INTEGER PRIMARY KEY,
        driver INTEGER NOT NULL REFERENCES driver(id) ON DELETE CASCADE,
        arch TEXT NOT NULL
    );

    CREATE TABLE fileset_file (
        fileset INTEGER NOT NULL REFERENCES fileset(id) ON DELETE CASCADE,
        -- relative to driver.root
        path TEXT NOT NULL,
        size INTEGER,
        PRIMARY KEY (fileset, path)
    );

    CREATE TABLE target (
        id INTEGER PRIMARY KEY,
        driver INTEGER NOT NULL REFERENCES driver(id) ON DELETE CASCADE,
        -- NULL for built-in drivers of a WIM
        fileset INTEGER REFERENCES fileset(id) ON DELETE CASCADE,
        -- the Models section listing this target (its target OS decoration)
        models INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
        hwid TEXT NOT NULL COLLATE NOCASE,
        -- 'hardware' or 'compatible'
        kind TEXT NOT NULL,
        name TEXT,
        install_section TEXT,
        -- same as models.arch, for lookups by hardware id and architecture
        arch TEXT NOT NULL,
        -- DriverVer of the DDInstall section (or [Version]), date as YYYY-MM-DD
        date TEXT,
        v_major INTEGER,
        v_minor INTEGER,
        v_patch INTEGER,
        v_build INTEGER
    );

    CREATE INDEX target_lookup ON target(hwid, arch);
    CREATE INDEX target_driver ON target(driver);
    CREATE INDEX fileset_driver ON fileset(driver);
    CREATE INDEX models_driver ON models(driver);
"""


def versionParts(version: DriverVersion | None) -> tuple[int | None, int | None, int | None, int | None]:
    # DriverVer versions have four parts (w.x.y.z), missing parts are 0
    if version is None:
        return (None, None, None, None)
    w, x, y, z = (version.release + (0, 0, 0, 0))[:4]
    return (w, x, y, z)


class DriverDatabase:
    db_path: str
    conn: sqlite3.Connection

    def __init__(self, db_path: str):
        self.db_path = db_path

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        self._create_tables()
        # CRITICAL: SQLite requires this pragma to respect ON DELETE CASCADE
        self.conn.execute("PRAGMA foreign_keys = ON")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _create_tables(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return

        tables = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        if tables:
            # there is no migration, the drivers have to be scanned again
            print(f"warning: database schema version {version} is outdated, recreating {self.db_path} (all drivers have to be scanned again)")
        script = "".join(f"DROP TABLE IF EXISTS {table};\n" for table in tables)
        self.conn.executescript(f"BEGIN; {script} {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")

    def removeContainer(self, container: str):
        if container:
            self.conn.execute("DELETE FROM driver WHERE container = ?", (container,))
            self.conn.execute("DELETE FROM container WHERE guid = ?", (container,))

    def addContainer(self, wim: "WimImage"):
        self.conn.execute("""
            INSERT OR REPLACE INTO container (guid, path, image, arch, os_major, os_minor, os_build, product_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wim.guid, wim.path, wim.image, wim.arch, wim.osMajor, wim.osMinor, wim.osBuild, wim.productType))

    def addModels(self, driver: int, models: ModelsDecoration) -> int | None:
        cursor = self.conn.execute("""
            INSERT INTO models (driver, section, decoration, arch, os_major, os_minor, os_product_type, os_suite_mask, os_build)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (driver, models.section.lower(), models.decoration, models.arch, models.osMajor, models.osMinor,
             models.productType, models.suiteMask, models.build))
        return cursor.lastrowid

    def removeDriver(self, sha256: str, container: str):
        self.conn.execute("DELETE FROM driver WHERE sha256 = ? AND container = ?", (sha256, container))

    def addDriver(self, driver: "DriverFile", root: str, inf: str, container: str) -> int | None:
        cursor = self.conn.execute("""
            INSERT INTO driver (sha256, container, root, inf, class, provider, signed, date, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (driver.sha256, container, root, inf, driver.klass, driver.provider, int(driver.signed),
             driver.date.isoformat() if driver.date else None, str(driver.version) if driver.version else None))
        return cursor.lastrowid

    def addFileset(self, driver: int, arch: str, files: list[tuple[str, int | None]]) -> int | None:
        cursor = self.conn.execute("INSERT INTO fileset (driver, arch) VALUES (?, ?)", (driver, arch))
        self.conn.executemany("INSERT INTO fileset_file (fileset, path, size) VALUES (?, ?, ?)",
                              [(cursor.lastrowid, path, size) for path, size in files])
        return cursor.lastrowid

    def addTarget(self, driver: int, fileset: int | None, models: int, target: DriverTarget) -> int | None:
        cursor = self.conn.execute("""
            INSERT INTO target (driver, fileset, models, hwid, kind, name, install_section, arch,
                                date, v_major, v_minor, v_patch, v_build)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (driver, fileset, models, target.HardwareID, target.Kind, target.DeviceDescription, target.InstallSection,
             target.Architecture, target.date.isoformat() if target.date else None, *versionParts(target.version)))
        return cursor.lastrowid

    def close(self):
        self.conn.close()


# WINDOWS/ARCH of the WIM XML metadata (PROCESSOR_ARCHITECTURE_*)
WIM_ARCHITECTURES = {0: "x86", 5: "arm", 6: "ia64", 9: "amd64", 12: "arm64"}
# WINDOWS/PRODUCTTYPE of the WIM XML metadata (VER_NT_*)
WIM_PRODUCT_TYPES = {"winnt": 1, "lanmannt": 2, "servernt": 3}


class WimImage:
    """GUID and Windows version of a WIM image (wimlib-imagex info)."""
    path: str
    image: int
    guid: str
    arch: str | None
    osMajor: int | None
    osMinor: int | None
    osBuild: int | None
    productType: int | None

    def __init__(self, path: str, image: int):
        self.path = path
        self.image = image
        self.guid = ""
        self.arch = None
        self.osMajor = self.osMinor = self.osBuild = self.productType = None

        for line in subprocess.run(["wimlib-imagex", "info", path, "--header"], capture_output=True).stdout.splitlines():
            line = line.decode("utf-8").strip()
            if line.lower().startswith("guid"):
                self.guid = line[line.find("=") + 1:].strip().lower()
                break

        data = subprocess.run(["wimlib-imagex", "info", path, "--xml"], capture_output=True).stdout
        if not data:
            return
        # the XML metadata is UTF-16 LE with a byte order mark
        root = ElementTree.fromstring(data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8"))
        windows = root.find(f"IMAGE[@INDEX='{image}']/WINDOWS")
        if windows is None:
            return

        def number(tag: str) -> int | None:
            value = windows.findtext(tag)
            return int(value) if value and value.strip().isdigit() else None

        arch = number("ARCH")
        self.arch = WIM_ARCHITECTURES.get(arch) if arch is not None else None
        self.osMajor, self.osMinor, self.osBuild = number("VERSION/MAJOR"), number("VERSION/MINOR"), number("VERSION/BUILD")
        self.productType = WIM_PRODUCT_TYPES.get((windows.findtext("PRODUCTTYPE") or "").strip().lower())


def extractWimInfs(path: str, image: int, destination: str) -> list[pathlib.Path]:
    """
    Previously infs were extracted from WIMs using wimdir and a subsequent wiminfo which was slow
    but didn't require a temporary directory.
    It took several minutes for a WIM though and using a in-memory filesystem instead speeds
    up the process significantly and outweights the benefit of not having to cleanup a temporary directory.
    """
    env = os.environ.copy()
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    result = subprocess.run(["wimextract", path, str(image), "/Windows/INF/*.inf", f"--dest-dir={destination}", "--no-acls", "--nullglob"],
                            capture_output=True, env=env)
    if result.returncode != 0:
        print(f"error: extracting the INF files of {path} (image {image}) failed: {result.stderr.decode(errors='replace').strip()}")
        return []
    return sorted(p for p in pathlib.Path(destination).iterdir() if p.is_file() and p.suffix.lower() == ".inf")


def relativeTo(path: pathlib.Path, base: pathlib.Path) -> str:
    """path relative to base if it is inside of it, otherwise the absolute path"""
    path, base = path.absolute(), base.absolute()
    return path.relative_to(base).as_posix() if path.is_relative_to(base) else str(path)


def storePackage(driver: DriverFile, files: set[str], storeDir: pathlib.Path) -> pathlib.Path:
    """
    Copies the files (relative to the INF directory) into <storeDir>/<sha256[:16]>, keeping the
    layout of the package. The folder is content addressed: re-storing a package is a no-op and
    different versions of a driver never overwrite each other.
    """
    destination = storeDir.joinpath(driver.sha256[:16])
    for relative in sorted(files):
        source = driver.rootPath.joinpath(relative)
        target = destination.joinpath(relative)
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


def deviceKey(hwid: str, kind: str, models: ModelsDecoration, installSection: str) -> str:
    """
    A device of an INF as "ID|kind|arch|minimum OS|install section", the fields of 'infverif /info'
    (minimum OS: major.minor[.build] of the TargetOSVersion decoration, empty without version).
    """
    minimumOS = ""
    if models.osMajor is not None:
        minimumOS = f"{models.osMajor}.{models.osMinor or 0}" + (f".{models.build}" if models.build else "")
    return f"{hwid.upper()}|{kind}|{models.arch}|{minimumOS}|{installSection.lower()}"


def listDevices(infs: list[str]) -> dict:
    """Devices and ExcludeID-excluded ids per INF (used by infverif-compare.ps1)."""
    result = {}
    for path in infs:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                driver = DriverFile(path)
                if driver.valid:
                    driver.parseDevices()
        except Exception as e:
            result[path] = {"error": f"{type(e).__name__}: {e}"}
            continue
        result[path] = {
            "devices": sorted({deviceKey(t.HardwareID, t.Kind, t.Models, t.InstallSection) for t in driver.targets}),
            "excluded": sorted({deviceKey(*e) for e in driver.excluded}),
        }
    return result


def planUpload(infs: list[str], manifest: Manifest, classFilter: set[str] | None) -> dict:
    """
    Which files of an upload are required: the INF files are available locally, all other files are
    only known by the manifest. Returns the drivers with their required and missing files (relative
    to the manifest root) and the scanner messages, nothing is written to the database.
    """
    def relative(paths) -> list[str]:
        return sorted({manifest.relative(p) for p in paths if p.absolute().is_relative_to(manifest.root)})

    drivers = []
    required: set[str] = set()
    missing: set[str] = set()
    for path in infs:
        out = io.StringIO()
        skipped = None
        with contextlib.redirect_stdout(out):
            driver = DriverFile(path, manifest=manifest)
            if not driver.valid:
                skipped = "not a valid INF file"
            elif classFilter and driver.klass.lower() not in classFilter:
                skipped = f"class {driver.klass or '(none)'} is filtered"
            else:
                driver.parseDevices()
                if not driver.targets:
                    skipped = "no hardware ids"

        entry = {
            "inf": manifest.relative(pathlib.Path(path)),
            "class": driver.klass,
            "provider": driver.provider,
            "version": str(driver.version) if driver.version else None,
            "date": driver.date.isoformat() if driver.date else None,
            "signed": driver.signed,
            "skipped": skipped,
            "messages": out.getvalue().splitlines(),
        }
        if skipped is None:
            files = relative(f for t in driver.targets for f in t.files)
            driverMissing = relative(driver.missing)
            entry.update({
                "targets": len(driver.targets),
                "hardwareIds": len({t.HardwareID for t in driver.targets}),
                "architectures": sorted({t.Architecture or "" for t in driver.targets}),
                "files": files,
                "missing": driverMissing,
                "size": sum(manifest.sizes.get(manifest.root.joinpath(f)) or 0 for f in files),
            })
            required.update(files)
            missing.update(driverMissing)
        drivers.append(entry)

    return {
        "drivers": drivers,
        "required": sorted(required),
        "missing": sorted(missing),
        "size": sum(manifest.sizes.get(manifest.root.joinpath(f)) or 0 for f in required),
    }


def importDriver(db: DriverDatabase, driver: DriverFile, container: str, wim: bool, store: pathlib.Path | None):
    dbDir = pathlib.Path(db.db_path).absolute().parent

    # files of each target relative to the package root (the INF directory)
    targetFiles: dict[int, frozenset[str]] = {}
    if not wim:
        for index, t in enumerate(driver.targets):
            relative: set[str] = set()
            for f in t.files:
                if f.absolute().is_relative_to(driver.rootPath.absolute()):
                    relative.add(f.absolute().relative_to(driver.rootPath.absolute()).as_posix())
                else:
                    print(f"error: {f} is outside of the driver package {driver.rootPath}")
            targetFiles[index] = frozenset(relative)

    root = driver.rootPath
    if store and not wim:
        root = storePackage(driver, set().union(*targetFiles.values()), store)
    rootValue = str(root) if wim else relativeTo(root, dbDir)

    with db.conn:
        db.removeDriver(driver.sha256, container)
        driverId = db.addDriver(driver, rootValue, driver.infPath.name, container)
        if driverId is None:
            raise RuntimeError(f"failed to add driver {driver.infPath}")

        modelsIds = {id(models): db.addModels(driverId, models) for models in driver.models}

        # one file set per architecture: all files the INF requires for it. Importing a driver
        # package into the driver store (drvload, pnputil, DISM) fails if a file of another
        # install section of the same architecture is missing ("The system cannot find the file
        # specified"), so the files of a single install section are not enough.
        archFiles: dict[str, set[str]] = {}
        for index, t in enumerate(driver.targets):
            archFiles.setdefault(t.Architecture or "", set()).update(targetFiles.get(index, ()))
        filesets: dict[str, int | None] = {}

        for t in driver.targets:
            filesetId = None
            arch = t.Architecture or ""
            if not wim:
                if arch not in filesets:
                    sizes = [(f, root.joinpath(f).stat().st_size if root.joinpath(f).is_file() else None) for f in sorted(archFiles[arch])]
                    filesets[arch] = db.addFileset(driverId, arch, sizes)
                filesetId = filesets[arch]
            modelsId = modelsIds[id(t.Models)]
            if modelsId is None:
                raise RuntimeError(f"failed to add models section {t.Models.sectionName()}")
            db.addTarget(driverId, filesetId, modelsId, t)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="INF File Parser for Windows Drivers"
    )

    parser.add_argument(
        "--database", 
        help="Path to the output file (default: %(default)s)",
        default="drivers.sqlite3",
        metavar="dbfile"
    )

    parser.add_argument(
        "--container",
        help="add scanned drivers to a container (wim's guid is used by default for built-in scans)"
    )

    parser.add_argument(
        "--store",
        action="store_true",
        help="copy the minimal driver package (all files required by its targets) to <store-dir>/<sha256 of the INF>"
    )

    parser.add_argument(
        "--store-dir",
        help="store directory (default: 'store' next to the database)"
    )

    parser.add_argument(
        "--wim-image",
        type=int,
        default=1,
        help="image index of WIM files (default: %(default)s)"
    )

    parser.add_argument(
        "--class-filter",
        help="only add drivers for the given class (e.g. Net, Display, Bluetooth)",
        nargs="+"
    )

    parser.add_argument(
        "--devices",
        action="store_true",
        help="print the devices of the INF files as JSON (no database changes), see infverif-compare.ps1"
    )

    parser.add_argument(
        "--plan",
        action="store_true",
        help="print which files of the driver packages are required as JSON (no database changes), "
             "files are resolved against --manifest"
    )

    parser.add_argument(
        "--manifest",
        help="JSON list of {\"path\": ..., \"size\": ...} of the driver package files for --plan"
    )

    parser.add_argument(
        "--manifest-root",
        help="directory the manifest paths are relative to (default: the manifest's directory)"
    )

    parser.add_argument(
        "--init",
        action="store_true",
        help="create the database (or recreate an outdated one) and exit"
    )

    parser.add_argument(
        "files", 
        help="One or more Windows INF or WIM files (for built-in scans) to parse",
        nargs="*",
        metavar="filepath"
    )

    args = parser.parse_args()
    if args.init:
        with DriverDatabase(args.database):
            print(f"database {args.database} (schema version {SCHEMA_VERSION}) is ready")
        sys.exit(0)
    if not args.files:
        parser.error("the following arguments are required: filepath")
    classFilter = {c.lower() for c in args.class_filter} if args.class_filter else None
    storeDir = None
    if args.store:
        storeDir = pathlib.Path(args.store_dir) if args.store_dir else pathlib.Path(args.database).absolute().parent.joinpath("store")

    def scanInf(db: DriverDatabase, path: str, container: str, data: bytes | None = None):
        driver = DriverFile(path, data=data)
        if not driver.valid:
            return

        # class names are case-insensitive
        if classFilter and driver.klass.lower() not in classFilter:
            return

        driver.parseDevices()
        if len(driver.targets) == 0:
            with db.conn:
                db.removeDriver(driver.sha256, container)
            print(path + " - no hardware ids contained - ignoring")
            return

        print("processing: " + path)
        # we do not store files of built-in drivers as the use-case for wim-parsing
        # is to check if driver for a device is built-in and not to download those
        # files (also, drivers are not stored in /Windows/INF but in
        # /Windows/System32/DriverStore which requires a different parsing strategy)
        importDriver(db, driver, container, data is not None, storeDir)

    if args.devices:
        print(json.dumps(listDevices(args.files), indent=1))
        sys.exit(0)

    if args.plan:
        if not args.manifest:
            parser.error("--plan requires --manifest")
        print(json.dumps(planUpload(args.files, Manifest.load(args.manifest, args.manifest_root), classFilter), indent=2))
        sys.exit(0)

    with DriverDatabase(args.database) as db:
        tempDir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
        for path in args.files:
            container = args.container or ""
            if os.path.splitext(path)[1].lower() != ".wim":
                scanInf(db, path, container)
                continue

            image = args.wim_image
            print(f"processing: {path} (image={image})")
            wimImage = WimImage(path, image)
            container = container or wimImage.guid
            if not container:
                print(f"error: could not read the GUID of {path}")
                continue
            wimImage.guid = container
            if not wimImage.arch:
                print(f"warning: could not read the architecture and version of image {image} of {path}")
            # drivers removed from the image must not remain in the database
            with db.conn:
                db.removeContainer(container)
                db.addContainer(wimImage)

            # the INF files are only needed while scanning, keep them in memory where possible
            with tempfile.TemporaryDirectory(prefix="windrv-", dir=tempDir) as tmp:
                for inf in extractWimInfs(path, image, tmp):
                    scanInf(db, f"/Windows/INF/{inf.name}", container, inf.read_bytes())
