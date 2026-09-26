#!/srv/windrv/.venv/bin/python
import os
import shutil
import subprocess
import argparse
import pathlib
import infparser
import sqlite3
from packaging import version as pkgver
from datetime import date, datetime


class DriverTarget:
    HardwareID: str
    DeviceDescription: str
    date: date | None
    version: pkgver.Version | None
    Architecture: str | None
    OSMajorVersion: int | None
    OSMinorVersion: int | None
    ProductType: str | None
    SuiteMask: str | None
    BuildNumber: int | None
    root: pathlib.Path
    files: set[pathlib.Path]

    def __init__(self, hardwareID: str, deviceDescription: str, date: date | None, version: pkgver.Version | None, files: list[pathlib.Path]):
        self.HardwareID = hardwareID
        self.DeviceDescription = deviceDescription
        self.date = date
        self.version = version
        self.Architecture = self.OSMajorVersion = self.OSMinorVersion = self.ProductType = self.SuiteMask = self.BuildNumber = None

        if len(files) == 1:
            # if there is only one file, the commonpath would be the file itself which we need to prevent
            self.root = files[0].parent
        else:
            self.root = pathlib.Path(os.path.commonpath(files))

        self.files = { p.relative_to(self.root) for p in files }


class DriverFile:
    valid: bool
    klass: str
    infPath: pathlib.Path
    rootPath: pathlib.Path
    version: pkgver.Version
    date: date | None
    targets: list[DriverTarget]
    inf: infparser.InfFile
    sections: dict[str, list[tuple[str, str, infparser.Entry]]]
    catalogs: dict[str, set[pathlib.Path]]
    sectionFiles: dict[str, set[str]]
    # tuple(arch, diskid)
    sourceDiskNames: dict[tuple[str, str], pathlib.Path]
    # tuple(arch, filename)
    sourceDiskFiles: dict[tuple[str, str], pathlib.Path]
    # key to value mapping for string replacement (e.g. %strkey% in the model section)
    strings: dict[str, str]

    def __init__(self, path: str, wim: tuple[str, int] | None = None):
        self.valid = False
        self.klass = ""
        self.infPath = pathlib.Path(path)
        self.rootPath = self.infPath.parent
        self.sections = {}
        self.catalogs = {}
        self.sectionFiles = {}
        self.sourceDiskNames = {}
        self.sourceDiskFiles = {}
        self.strings = {}
        self.targets = []

        if wim:
            data = subprocess.run(["wimextract", wim[0], str(wim[1]), path, "--to-stdout"], capture_output=True).stdout
            self.inf = infparser.InfFile.load(data, name=path)
        else:
            self.inf = infparser.InfFile.load(path)

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

        self._parseVersion()
        self.valid = True


    def parseDevices(self):
        self._parseSourceFiles()
        self._parseStrings()
        self._parseManufacturer()

        if len(self.targets):
            root = pathlib.Path(os.path.commonpath([p.root for p in self.targets]))
            self.infPath = self.infPath.relative_to(root)
            self.rootPath = root
            for t in self.targets:
                t.root = t.root.relative_to(self.rootPath)


    def _parseSourceFiles(self):
        for section in self.sections:
            if not section.startswith("sourcedisksnames"):
                continue

            arch = section[section.find('.') + 1:] if '.' in section else ''
            for diskid, value, _ in self.sections[section]:
                value = value.split(',')
                if arch not in self.catalogs: self.catalogs[arch] = set()

                # diskid = disk-description[,tag-or-cab-file] |
                # diskid = disk-description[,[tag-or-cab-file][,[unused][,path]]] |
                # diskid = disk-description[,[tag-or-cab-file],[unused],[path][,flags]] |
                # diskid = disk-description[,[tag-or-cab-file],[unused],[path],[flags][,tag-file]]  (Windows XP and later versions of Windows)
                (desc, tagOrCabFile, _, path, flags, tagFile) = value + [''] * (6 - len(value))

                if (arch, diskid) in self.sourceDiskNames:
                    print(f"warning: diskid {diskid} defined path multiple times (section: {section})")

                if path:
                    path = path.strip('"')
                    path = pathlib.Path(pathlib.PureWindowsPath(path).as_posix())
                    if path.is_absolute():
                        path = pathlib.Path(*path.parts[1:])
                    self.sourceDiskNames[(arch, diskid)] = self.rootPath.joinpath(path)
                else:
                    self.sourceDiskNames[(arch, diskid)] = self.rootPath

                if tagOrCabFile:
                    tagPath = self.sourceDiskNames[(arch, diskid)].joinpath(tagOrCabFile)
                    if not arch in self.catalogs: self.catalogs[arch] = set()
                    if tagPath in self.catalogs[arch]:
                        print(f"warning: tag-or-cab-file {tagOrCabFile} appeared multiple times (section: {section})")
                    else:
                        self.catalogs[arch].add(tagPath)

                if tagFile:
                    tagPath = self.sourceDiskNames[(arch, diskid)].joinpath(tagFile)

                    if not arch in self.catalogs: self.catalogs[arch] = set()
                    if tagPath in self.catalogs[arch]:
                        print(f"warning: tag-file {tagFile} appeared multiple times (section: {section})")
                    else:
                        self.catalogs[arch].add(tagPath)

        for section in self.sections:
            if not section.startswith("sourcedisksfiles"):
                continue

            arch = section[section.find('.') + 1:] if '.' in section else ''
            for filename, value, _ in self.sections[section]:
                if (arch, filename) in self.sourceDiskFiles:
                    print(f"warning: {filename} path already configured for {arch}")

                value = value.split(',')
                # filename=diskid[,[ subdir][,size]]
                (diskid, subdir, size) = value + [''] * (3 - len(value))

                basepath = None
                if (arch, diskid) in self.sourceDiskNames:
                    basepath = self.sourceDiskNames[(arch, diskid)]
                elif ('', diskid) in self.sourceDiskNames:
                    basepath = self.sourceDiskNames[('', diskid)]
                else:
                    basepath = self.rootPath

                if subdir:
                    subdir = pathlib.Path(pathlib.PureWindowsPath(subdir).as_posix())
                    if subdir.is_absolute():
                        subdir = pathlib.Path(*subdir.parts[1:])
                    self.sourceDiskFiles[(arch, filename)] = basepath.joinpath(subdir)
                else:
                    self.sourceDiskFiles[(arch, filename)] = basepath


    def _parseStrings(self):
        # the parser selects the Strings section and unquotes its values
        self.strings = self.inf.strings


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
        for key, value, _ in self.sections[name]:
            key = key.lower()
            filesOrSection = value.lower().split(',')
            # ignoring "include":
            # this is for system supplied INF files which therefore are fully
            # included in the system already
            # 
            # ignoring "needs":
            # this is for sections in system supplied INF files which are
            # means all the files are already present in the system
            if key == "copyinf":
                for f in filesOrSection:
                    files.add(f.strip())
            elif key == "copyfiles":
                for f in filesOrSection:
                    f = f.strip()
                    if f.startswith('@'):
                        files.add(f[1:])
                    else:
                        if f not in self.sections:
                            print("error: section not found: " + f)
                            continue

                        # file-list-section only contain line separated file lists
                        for fileEntry, _, _ in self.sections[f.lower()]:
                            files.add(fileEntry.split(',')[0])

        stack.remove(name)
        self.sectionFiles[name] = files
        return files


    def _parseModelSection(self, section: str, TargetOSVersion: str, target: tuple[str | None, str | None, str | None, str | None, str | None, str | None]):
        section = section.lower()
        TargetOSVersion = TargetOSVersion.lower()

        # A per-manufacturer Models section identifies at least one device,
        # references the DDInstall section of the INF file for that device,
        # and specifies a unique-to-the-model-section Hardware identifier (ID) for that device.

        (arch, osma, osmi, product, suite, build) = target
        arch = arch.lower() if arch else None

        modelSections: list[str] = []
        if arch and f"{section}.{TargetOSVersion}" in self.sections:
            modelSections.append(f"{section}.{TargetOSVersion}")
        elif section in self.sections:
            modelSections.append(section)

        if len(modelSections) == 0:
            print(f"error: model section {section} not found")
            return

        # device-description=install-section-name[,hw-id][,compatible-id...]
        # The description belongs to the model entry, i.e. to the pair of install
        # section and device id. The same id may be listed by several entries;
        # Windows treats each entry as a separate driver candidate, so every
        # (id, install section) pair becomes its own target.
        deviceSections: dict[tuple[str, str], str] = {}
        for modelSection in modelSections:
            for _, installSectionAndDeviceID, entry in self.sections[modelSection]:
                if not installSectionAndDeviceID or not ',' in installSectionAndDeviceID:
                    continue

                parts = installSectionAndDeviceID.split(',')
                installSection = parts[0].strip().lower()
                if f"{installSection}.{arch}" in self.sections: installSection = f"{installSection}.{arch}"
                elif f"{installSection}.nt" in self.sections: installSection = f"{installSection}.nt"
                elif installSection not in self.sections:
                    print(f"error: device section {installSection} not found - {installSectionAndDeviceID}")
                    continue

                # the parser already expanded %strkey% tokens (e.g. %DeviceDesc%)
                deviceDescription = entry.key or ""

                hardwareIDs = parts[1:]
                for id in hardwareIDs:
                    id = id.strip().upper() # devmgmt.msc always shows only upper cased hardware ids (?)
                    if not id: continue
                    if (id, installSection) in deviceSections:
                        if deviceSections[(id, installSection)] != deviceDescription:
                            print(f"warning: {id} listed multiple times for {installSection} in [{modelSection}] (line {entry.line}), keeping the first description")
                        continue
                    deviceSections[(id, installSection)] = deviceDescription

        for (hardwareId, installSection), name in deviceSections.items():
            sections: set[str] = set()
            if arch and f"{installSection}.{arch}" in self.sections:
                sections.add(f"{installSection}.{arch}")
            elif f"{installSection}.nt" in self.sections:
                sections.add(f"{installSection}.nt")
            elif installSection in self.sections:
                sections.add(installSection)
            else:
                print(f"error: device section {installSection} not found")
                continue

            for subtype in ["coinstallers", "services", "interfaces", "hw", "events", "components", "com"]:
                # coinstallers is deprecated for win10
                if arch and f"{installSection}.{arch}.{subtype}" in self.sections:
                    sections.add(f"{installSection}.{arch}.{subtype}")
                elif f"{installSection}.nt.{subtype}" in self.sections:
                    sections.add(f"{installSection}.nt.{subtype}")
                elif f"{installSection}.{subtype}" in self.sections:
                    sections.add(f"{installSection}.{subtype}")

            files: set[str] = set()
            for sectionName in sections:
                files = files.union(self._getSectionFiles(sectionName))

            mappedfiles: set[pathlib.Path] = {self.infPath}
            for f in files:
                if arch and (arch, f) in self.sourceDiskFiles:
                    mappedfiles.add(self.sourceDiskFiles[(arch, f)].joinpath(f))
                elif ('', f) in self.sourceDiskFiles:
                    mappedfiles.add(self.sourceDiskFiles[('', f)].joinpath(f))
                else:
                    mappedfiles.add(self.rootPath.joinpath(f))

            if arch and arch in self.catalogs:
                mappedfiles = mappedfiles.union(self.catalogs[arch])
            elif '' in self.catalogs:
                mappedfiles = mappedfiles.union(self.catalogs[''])

            device = DriverTarget(hardwareId, name, self.date, self.version, list(mappedfiles))
            device.Architecture = arch
            device.OSMajorVersion = int(osma) if osma else None
            device.OSMinorVersion = int(osmi) if osmi else None
            device.ProductType = product
            device.SuiteMask = suite
            device.BuildNumber = int(build) if build else None
            self.targets.append(device)


    def _parseManufacturer(self, section:str = "manufacturer"):
        if not section in self.sections: return
        # https://learn.microsoft.com/windows-hardware/drivers/install/inf-manufacturer-section
        for manufacturer, value, _ in self.sections[section]:
            # manufacturer-name |
            # %strkey%=models-section-name |
            # %strkey%=models-section-name [,TargetOSVersion] [,TargetOSVersion] ...  (Windows XP and later versions of Windows)
            value = value.strip().lower()

            if not value:
                # manufacturer-identifier
                self._parseManufacturer(manufacturer)
                continue

            modelSection = targets = None
            if ',' not in value:
                # TODO: add support for DOS kernel handling
                # currently, an empty TargetOSVersion simply defaults to regular fallbacks being used
                modelSection = value
                targets = ['']
            else:
                values = [v.strip() for v in value.split(',')]
                modelSection = values[0]
                targets = values[1:]

            for TargetOSVersion in targets:
                # Windows XP to Windows 10 v1511:
                # NT[Architecture][.[OSMajorVersion][.[OSMinorVersion][.[ProductType][.SuiteMask]]]]
                # since Windows 10 v1607 (build 14310):
                # NT[Architecture][.[OSMajorVersion][.[OSMinorVersion][.[ProductType][.[SuiteMask][.[BuildNumber]]]]]
                parts = []
                if '.' in TargetOSVersion:
                    parts = TargetOSVersion.split('.')
                elif TargetOSVersion:
                    parts = [TargetOSVersion]

                (arch, osma, osmi, product, suite, build) = parts + [None] * (6 - len(parts))
                # not sure if the os specific e.g. [StdMfg.NTamd64] is automatically checked by windows 
                # even though it is not part explictily mentioned for the model section
                self._parseModelSection(modelSection, TargetOSVersion, (arch, osma, osmi, product, suite, build))


    def _parseVersion(self):
        # TODO: Implement support for "LayoutFile"
        for key, value, _ in self.sections["version"]:
            if key == "class":
                self.klass = value
            elif key.startswith("catalogfile"):
                arch = key[key.find("catalogfile.") + 1:] if '.' in key else ''
                if not arch in self.catalogs: self.catalogs[arch] = set()
                self.catalogs[arch].add(self.rootPath.joinpath(value))
            elif key == "driverver":
                # mm/dd/yyyy,w.x.y.z
                dateValue = version = None
                if not ',' in value:
                    print(f"warning: version does not conform to spezification, mm/dd/yyyy,w.x.y.z expected, got {value}")
                    dateValue = value
                else:
                    dateValue, version = value.split(',')

                dateValue = dateValue.strip() # note: strip should not be necessary for compliant files
                if version:
                    try:
                        self.version = pkgver.Version(version)
                    except:
                        print("error: invalid version value: " + version)

                try:
                    if dateValue == "00/00/0000":
                        self.date = None
                    elif '/' in dateValue:
                        self.date = datetime.strptime(dateValue, "%m/%d/%Y").date()
                    elif '-' in dateValue:
                        self.date = datetime.strptime(dateValue, "%m-%d-%Y").date()
                    else:
                        print("warning: unknown date format: " + dateValue)
                except ValueError:
                    print("error: invalid date value: " + dateValue)

class DriverDatabase:
    db_path: str

    def __init__(self, db_path: str):
        self.db_path = db_path

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        # CRITICAL: SQLite requires this pragma to respect ON DELETE CASCADE
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._create_tables()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _create_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS driver (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root TEXT NOT NULL,
                    inf TEXT NOT NULL,
                    container TEXT
                )""")

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS target (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    driver INTEGER NOT NULL,
                    root TEXT NOT NULL,
                    hwid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arch TEXT,
                    os_major INTEGER,
                    os_minor INTEGER,
                    os_build INTEGER,
                    date INTEGER,
                    v_major INTEGER,
                    v_minor INTEGER,
                    v_patch INTEGER,
                    v_build INTEGER,
                    FOREIGN KEY (driver) REFERENCES driver(id) ON DELETE CASCADE
                )""")

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS file (
                    target INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    FOREIGN KEY (target) REFERENCES target(id) ON DELETE CASCADE
                )""")

            self.conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_target_lookup 
                ON target(hwid, arch, os_major, os_minor, os_build)
            ''')

    def addDriver(self, id: int | None, root: str, inf: str, container: str | None) -> int | None:
        with self.conn:
            if id:
                self.conn.execute("INSERT OR IGNORE INTO driver (id, root, inf, container) VALUES (?, ?, ?, ?)", (id, root, inf, container if container else ""))
            else:
                cursor = self.conn.execute("INSERT INTO driver (root, inf, container) VALUES (?, ?, ?)", (root, inf, container if container else ""))
                id = cursor.lastrowid

            return id

    def removeContainer(self, container: str):
        if container:
            with self.conn:
                self.conn.execute("DELETE FROM driver WHERE container=?", (container,))

    def removeDriver(self, root: str, inf: str, container: str):
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM driver WHERE root = ? AND inf = ? AND container = ? RETURNING id",
                (root, inf, container)
            )
            res = cursor.fetchall()
            if res: return res[0][0]

    def addTarget(self, driver: int, root: str, hwid: str, name: str, arch: str | None, os_major: int | None, os_minor: int | None, os_build: int | None, date: date | None, version: pkgver.Version | None) -> int | None:
        # DriverVer versions have four parts (w.x.y.z), missing parts are 0
        v_major, v_minor, v_patch, v_build = (version.release + (0, 0, 0, 0))[:4] if version else (None, None, None, None)
        timestamp = datetime.combine(date, datetime.min.time()).timestamp() if date else None
        with self.conn:
            cursor = self.conn.execute("""
                INSERT INTO target (driver, root, hwid, name, arch, os_major, os_minor, os_build, date, v_major, v_minor, v_patch, v_build)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (driver, root, hwid, name, arch, os_major, os_minor, os_build, timestamp, v_major, v_minor, v_patch, v_build))
            return cursor.lastrowid

    def addFile(self, target: int, path: str) -> int | None:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO file (target, path) VALUES (?, ?)", 
                (target, path)
            )
            return cursor.lastrowid

    def close(self):
        self.conn.close()


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
        help="copy driver files to the given folder"
    )

    parser.add_argument(
        "--class-filter",
        help="only add drivers for the given class (e.g. Net, Display, Bluetooth)",
        nargs="+"
    )

    parser.add_argument(
        "files", 
        help="One or more Windows INF or WIM files (for built-in scans) to parse",
        nargs="+",
        metavar="filepath"
    )

    args = parser.parse_args()
    with DriverDatabase(args.database) as db:
        env = os.environ.copy()
        env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"

        for path in args.files:
            _, ext = os.path.splitext(path)
            ext = ext.lower()

            files: list[str] = []
            container = args.container
            wim = None
            if ext == ".wim":
                image = 1
                wim = (path, image)
                print(f"processing: {path} (image={image})")
                for file in subprocess.run(["wimdir", path, str(image), "--path=/Windows/INF"], capture_output=True, env=env).stdout.splitlines():
                    file = file.decode("utf-8")
                    if file.lower().endswith(".inf"): files.append(file)

                if not container:
                    for line in subprocess.run(["wimlib-imagex", "info", path, "--header"], capture_output=True).stdout.splitlines():
                        line = line.decode("utf-8").lstrip().lower()
                        if line.startswith("guid"):
                            container = line[line.find("=") + 1:].strip()
                            break
            else:
                files.append(path)

            for file in files:
                driver = DriverFile(file, wim=wim)
                if not driver.valid: continue

                if args.class_filter and driver.klass not in args.class_filter:
                    continue

                driver.parseDevices()

                # only now the infPath and rootPath are now accurate
                drvid = db.removeDriver(str(driver.rootPath), str(driver.infPath), container if container else "")
                if len(driver.targets) == 0:
                    print(file + " - no hardware ids contained - ignoring")
                    continue

                print("processing: " + file)
                rootPath = str(driver.rootPath)
                if args.store and not wim:
                    rootPath = os.path.join('store', args.store)
                    os.makedirs(rootPath, exist_ok=True)

                drvid = db.addDriver(drvid, rootPath, str(driver.infPath), container)
                if not drvid:
                    print(f"error: failed to add driver {file}")
                    continue

                for d in driver.targets:
                    tid = db.addTarget(drvid, str(d.root), d.HardwareID, d.DeviceDescription, d.Architecture, d.OSMajorVersion, d.OSMinorVersion, d.BuildNumber, d.date, d.version)
                    if not tid:
                        print(f"error: failed to add target {d.HardwareID} for {file}")
                        continue

                    if not wim:
                        # we do not store built-in drivers as the use-case for wim-parsing
                        # is to check if driver for a device is built-in and not
                        # to download those files (also, drivers are not store in /Windows/INF
                        # but in /Windows/System32/DriverStore which requires a different 
                        # parsing strategy)
                        for fpath in d.files:
                            db.addFile(tid, str(fpath))
                            if args.store:
                                shutil.copy(driver.rootPath.joinpath(fpath), os.path.join('store', args.store, fpath))
