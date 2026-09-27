# What is this about?
The main purpose of this software is to provide dynamic network card driver injection on a iPXE / wimboot / WinPE setup.
In essence, the windrvscan.py parses a .inf file and stores (hardware-id, windows, file) tuples in a sqlite3 database.

The logic data layout is as follows:
- driver (sha256 of the INF, container, package root, INF, class, provider, signed, DriverVer)
- fileset (driver, architecture) with its files: the files a target needs, shared by all targets with the same files
- target (driver, fileset, hardware-id, hardware/compatible, architecture, target OS version, DriverVer)

A driver can be tagged with a container which is done automatically when processing wim files - in this case the container is the wim's guid. The allows a iPXE boot script to only inject a driver not contained by the boot/install wim. Drivers of a container have targets but no files.


# Setup
```sh
apt install wimtools
cd /srv && git clone https://github.com/kr4uzi/windrv
```

If you didn't install to /srv/windrv please adjust the first line of windrvscan.py!

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install packaging
chmod +x windrvscan.py

# make windrv available for pxe
ln -s /srv/windrv /var/www/html/windrv
```

Note: Package folders are stored relative to the database directory if they are inside of it, otherwise as absolute paths.

# Adding Builtin Drivers from WIMs
`/srv/windrv/windrvscan.py --database=/srv/windrv/drivers.sqlite3 --class-filter=Net boot.wim`

# Add and copy drivers from a directory
`/srv/windrv/windrvscan.py --database=/srv/windrv/drivers.sqlite3 --store /path/to/amd/drivers/driver.inf`

This will parse the driver.inf file but also copy the driver files required by its targets to `store/<sha256 of the INF>` next to the database (or `--store-dir`), keeping the package layout and storing only the absolute minimum files required. The folder is content addressed: storing the same package again is a no-op and different versions of a driver never overwrite each other.

`--fileset-scope section` (default) assigns each target the files of its install section, `--fileset-scope arch` all files of the driver for the target's architecture.

# API
`api.php` answers JSON and is the only interface to the database for other tools (e.g. the iPXE menu `pxe/win/menu.php`):

- `api.php?action=match&container=<WIM GUID>&nics[0][ven]=8086&nics[0][dev]=1533&nics[0][subven]=8086&nics[0][subsys]=0001&nics[0][rev]=03&nics[0][class]=020000]`
  returns per network adapter whether the WIM already has a driver (`covered`) and the best signed stored driver (`driver`, `inject`).
  The WIM has to be scanned with `windrvscan.py` first (its GUID, architecture and Windows version are stored as container).
  `&unsigned=1` also returns unsigned drivers (64-bit WinPE does not load them).
- `api.php?action=targets&id[]=123` returns the INF path inside of the downloaded package (`download.php?target=123`).

Matching follows Windows: the device ids are generated like the PCI bus driver does
([Identifiers for PCI Devices](https://github.com/MicrosoftDocs/windows-driver-docs/blob/staging/windows-driver-docs-pr/install/identifiers-for-pci-devices.md)),
only the Models section with the best applicable TargetOSVersion of each manufacturer is used and drivers are ranked by the matched id, signature, date and version.

The web UI (`index.php`) searches hardware id fragments (`DEV_1533`) or complete device ids as shown by the device manager (`PCI\VEN_8086&DEV_1533&SUBSYS_00018086&REV_03`).

Scanning a WIM uses image 1 by default (`--wim-image` for e.g. the edition of an install.wim).

# TODOs
- Add support for DOS kernel handling (Win98)
- Investigate `LayoutFile` ([Version], documented for Windows 2000/XP): used by system-supplied INFs instead of
  SourceDisksNames/Files (e.g. from an XP `I386` folder, no WIM); currently ignored, file locations then unresolved
- Add primitve Driver Support:
  -> Manufacturer section must *not* be present
  -> DefaultInstall.nt{amd64,...} must be present
- Decide the default of `--fileset-scope` (files of a device's download package):
  -> `section`: only the files of the device's install section (e.g. netathr10x.inf: 5 of 112 files)
  -> `arch`: all files of the INF for the architecture (always complete)
  -> Open question: does importing a package into the driver store (`drvload` in WinPE, `pnputil /add-driver`,
     `DISM /Add-Driver`, "Browse my computer") require all files the INF references, or only those of the used install section?
  -> Test: signed vendor network driver whose install sections copy different files, `pnputil /add-driver` of a
     full, an `arch` and a `section` package on a Windows VM (`pnputil /delete-driver oemNN.inf` in between),
     then confirm the winner with `drvload` in WinPE
  -> Afterwards remove the option and keep the working mode; the store folder (`--store`) always holds all files

# References
## About model section parsing:
https://learn.microsoft.com/windows-hardware/drivers/install/creating-inf-files-for-multiple-platforms-and-operating-systems\
On Windows Server 2003 Service Pack 1 (SP1) and later, INF files must decorate entries 
in the INF Models section with .ntia64, .ntarm, .ntarm64 or .ntamd64 platform extensions 
to specify non-x86 target operating system versions.

These platform extensions are not required in INF files for x86-based target 
operating system versions, but are strongly recommended. 
The same platform extension decoration or .nt platform extension is optional 
on all other sections that support platform extensions.

1. Windows checks for a section-name.nt<architecture> section and, if one exists, processes it.
   Windows checks for the .nt<architecture> extension in the INF file that is being processed 
   and in any included INF files (that is, any INF files that are included with Include entries).

2. If a section-name.nt<architecture> section does not exist, Windows checks for 
   a section-name.nt section in the INF file or any included INF files.
   If one exists, Windows processes the section-name.nt section.

3. If a section-name.nt section does not exist, Windows processes a section-name section 
   that does not include a platform extension.

## Include and Needs directives
Include=filename.inf[,filename2.inf]...
Specifies one or more additional system-supplied INF files that contain sections needed to install this device. If this entry is specified, usually so is a Needs entry.

Needs=inf-section-name[,inf-section-name]...
Specifies the named sections that must be processed during the installation of this device. Typically, such a named section is a DDInstall.Components section within a system-supplied INF file that is listed in an Include entry. However, it can be any section that is referenced within such a DDInstall.Components section of the included INF.

=> Used to include system-supplied INF files / sections, therefore not required to be stored in the database