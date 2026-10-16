<?php
// windrv matching logic, shared by api.php and index.php

class WindrvDatabaseMissing extends RuntimeException
{
}

/**
 * Device setup classes (Class= of the [Version] section) with their device category:
 * https://learn.microsoft.com/windows-hardware/drivers/install/system-defined-device-setup-classes-available-to-vendors
 * https://learn.microsoft.com/windows-hardware/drivers/install/system-defined-device-setup-classes-reserved-for-system-use
 * Class names are case-insensitive.
 */
const WINDRV_DEVICE_CLASSES = [
    "AudioProcessingObject" => "Audio Processing Objects (APOs)",
    "Battery" => "Battery Devices",
    "Biometric" => "Biometric Device",
    "Bluetooth" => "Bluetooth Devices",
    "Camera" => "Camera Device",
    "CDROM" => "CD-ROM Drives",
    "DiskDrive" => "Disk Drives",
    "Display" => "Display Adapters",
    "Extension" => "Extension INF",
    "FDC" => "Floppy Disk Controllers",
    "FloppyDisk" => "Floppy Disk Drives",
    "HDC" => "Hard Disk Controllers",
    "HIDClass" => "Human Interface Devices (HID)",
    "Dot4" => "IEEE 1284.4 Devices",
    "Dot4Print" => "IEEE 1284.4 Print Functions",
    "61883" => "IEEE 1394 Devices That Support the 61883 Protocol",
    "AVC" => "IEEE 1394 Devices That Support the AVC Protocol",
    "SBP2" => "IEEE 1394 Devices That Support the SBP2 Protocol",
    "1394" => "IEEE 1394 Host Bus Controller",
    "Image" => "Imaging Device",
    "Infrared" => "IrDA Devices",
    "Keyboard" => "Keyboard",
    "MediumChanger" => "Media Changers",
    "MTD" => "Memory Technology Driver",
    "Modem" => "Modem",
    "Monitor" => "Monitor",
    "Mouse" => "Mouse",
    "Multifunction" => "Multifunction Devices",
    "Media" => "Multimedia",
    "MultiportSerial" => "Multiport Serial Adapters",
    "Net" => "Network Adapter",
    "NetClient" => "Network Client",
    "NetService" => "Network Service",
    "NetTrans" => "Network Transport",
    "SecurityAccelerator" => "PCI SSL Accelerator",
    "PCMCIA" => "PCMCIA Adapters",
    "Ports" => "Ports (COM & LPT ports)",
    "Printer" => "Printers",
    "PNPPrinters" => "Printers, Bus-specific class drivers",
    "Processor" => "Processors",
    "SCSIAdapter" => "SCSI, RAID, and NVMe Controllers",
    "Securitydevices" => "Security Devices",
    "Sensor" => "Sensors",
    "SmartCardReader" => "Smart Card Readers",
    "SoftwareComponent" => "Software Component",
    "NvmeDisk" => "Storage Disks",
    "Volume" => "Storage Volumes",
    "System" => "System Devices",
    "TapeDrive" => "Tape Drives",
    "UCM" => "USB Connector Managers",
    "USBDevice" => "USB Device",
    "WCEUSBS" => "Windows CE USB ActiveSync Devices",
    "WPD" => "Windows Portable Devices (WPD)",
    // reserved for system use
    "Adapter" => "Adapter",
    "APMSupport" => "APM",
    "Computer" => "Computer",
    "Decoder" => "Decoders",
    "1394Debug" => "Host-side IEEE 1394 Kernel Debugger Support",
    "Enum1394" => "IEEE 1394 IP Network Enumerator",
    "NoDriver" => "No driver",
    "LegacyDriver" => "Non-Plug and Play Drivers",
    "Unknown" => "Other Devices",
    "PrinterUpgrade" => "Printer Upgrade",
    "PrintQueue" => "Print Queue",
    "SoftwareDevice" => "Software Device",
    "AudioEndpoint" => "Audio Endpoint",
    "Sound" => "Sound",
    "VolumeSnapshot" => "Storage Volume Snapshots",
    "USB" => "USB Bus Devices (hubs and host controllers)",
];

// database path of config.php, relative paths are relative to this directory
function windrvDbPath(array $config): string
{
    $db = $config["db"];
    return str_starts_with($db, '/') ? $db : __DIR__ . DIRECTORY_SEPARATOR . $db;
}

// command line to run windrvscan.py with the python of config.php
function windrvScanCommand(array $config, array $args): string
{
    return implode(' ', array_map('escapeshellarg', array_merge(
        [$config["python"] ?? "python3", __DIR__ . DIRECTORY_SEPARATOR . "windrvscan.py", "--database=" . windrvDbPath($config)], $args)));
}

// runs windrvscan.py without a shell, returns [exit code, stdout, stderr]
function windrvScan(array $config, array $args): array
{
    $command = array_merge([$config["python"] ?? "python3", __DIR__ . DIRECTORY_SEPARATOR . "windrvscan.py"], $args);
    $process = proc_open($command, [1 => ["pipe", "w"], 2 => ["pipe", "w"]], $pipes);
    if (!is_resource($process)) {
        return [-1, "", "could not start " . ($config["python"] ?? "python3")];
    }
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    return [proc_close($process), $stdout, $stderr];
}

// opens the database read-only; the database is created by windrvscan.py (--init or a scan)
function windrvOpenDb(array $config): PDO
{
    $path = windrvDbPath($config);
    if (!is_file($path)) {
        throw new WindrvDatabaseMissing("database {$path} does not exist, create it with: " . windrvScanCommand($config, ["--init"]));
    }
    $db = new PDO("sqlite:" . $path, null, null,
        // Pdo\Sqlite exists since PHP 8.4, the PDO::SQLITE_* constants are deprecated since 8.5
        class_exists('Pdo\Sqlite') ? [Pdo\Sqlite::ATTR_OPEN_FLAGS => Pdo\Sqlite::OPEN_READONLY]
                                   : [PDO::SQLITE_ATTR_OPEN_FLAGS => PDO::SQLITE_OPEN_READONLY]);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    return $db;
}

// hexadecimal field of a device id: without 0x prefix, upper case, zero padded
function windrvHex(?string $value, int $length): ?string
{
    $value = strtoupper(trim($value ?? ''));
    if (str_starts_with($value, '0X')) {
        $value = substr($value, 2);
    }
    if ($value === '' || !ctype_xdigit($value) || strlen($value) > $length) {
        return null;
    }
    return str_pad($value, $length, '0', STR_PAD_LEFT);
}

/**
 * Device identification strings the PCI bus driver reports for a device, in the order Windows
 * uses to rank them: hardware ids from specific to generic, then compatible ids.
 * https://learn.microsoft.com/windows-hardware/drivers/install/identifiers-for-pci-devices
 * $class is the 6 digit class code (base class, subclass, programming interface) or 4 digits.
 * Returns [[id, kind], ...] with kind 'hardware' or 'compatible', ids without duplicates.
 */
function pciDeviceIds(string $ven, string $dev, ?string $subven = null, ?string $subsys = null, ?string $rev = null, ?string $class = null): array
{
    $ven = windrvHex($ven, 4);
    $dev = windrvHex($dev, 4);
    if ($ven === null || $dev === null) {
        return [];
    }
    $subven = windrvHex($subven, 4);
    $subsys = windrvHex($subsys, 4);
    $rev = windrvHex($rev, 2);
    $class = trim($class ?? '');
    $cc = strlen($class) > 4 ? windrvHex($class, 6) : null;       // base class, subclass, interface
    $ccs = windrvHex(substr($class, 0, 4), 4);                     // base class, subclass

    $base = "PCI\\VEN_{$ven}&DEV_{$dev}";
    // SUBSYS_s(4)n(4): subsystem id followed by the subsystem vendor id
    $sub = ($subsys !== null && $subven !== null) ? "&SUBSYS_{$subsys}{$subven}" : null;

    $hardware = [];
    if ($sub !== null && $rev !== null) $hardware[] = $base . $sub . "&REV_{$rev}";
    if ($sub !== null) $hardware[] = $base . $sub;
    if ($rev !== null) $hardware[] = $base . "&REV_{$rev}";
    $hardware[] = $base;
    if ($cc !== null) $hardware[] = $base . "&CC_{$cc}";
    if ($ccs !== null) $hardware[] = $base . "&CC_{$ccs}";

    // the PCI Express device type (&DT_) is not known and therefore not generated
    $compatible = [];
    if ($rev !== null) $compatible[] = $base . "&REV_{$rev}";
    $compatible[] = $base;
    if ($cc !== null) $compatible[] = "PCI\\VEN_{$ven}&CC_{$cc}";
    if ($ccs !== null) $compatible[] = "PCI\\VEN_{$ven}&CC_{$ccs}";
    $compatible[] = "PCI\\VEN_{$ven}";
    if ($cc !== null) $compatible[] = "PCI\\CC_{$cc}";
    if ($ccs !== null) $compatible[] = "PCI\\CC_{$ccs}";

    $ids = [];
    $seen = [];
    foreach ([['hardware', $hardware], ['compatible', $compatible]] as [$kind, $list]) {
        foreach ($list as $id) {
            if (!isset($seen[$id])) {
                $seen[$id] = true;
                $ids[] = [$id, $kind];
            }
        }
    }
    return $ids;
}

/**
 * Parses a PCI device id as shown by the device manager, e.g.
 * PCI\VEN_8086&DEV_1533&SUBSYS_00008086&REV_03, into the arguments of pciDeviceIds().
 */
function parsePciDeviceId(string $id): ?array
{
    $id = strtoupper(trim($id));
    if (!preg_match('/^PCI\\\\VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})((?:&[A-Z]+_[0-9A-F]+)*)$/', $id, $m)) {
        return null;
    }
    $fields = ['ven' => $m[1], 'dev' => $m[2], 'subven' => null, 'subsys' => null, 'rev' => null, 'class' => null];
    preg_match_all('/&([A-Z]+)_([0-9A-F]+)/', $m[3], $parts, PREG_SET_ORDER);
    foreach ($parts as [, $key, $value]) {
        if ($key === 'SUBSYS' && strlen($value) === 8) {
            $fields['subsys'] = substr($value, 0, 4);
            $fields['subven'] = substr($value, 4, 4);
        } elseif ($key === 'REV') {
            $fields['rev'] = $value;
        } elseif ($key === 'CC') {
            $fields['class'] = $value;
        }
    }
    return $fields;
}

/**
 * Whether a Models section decoration applies to the target OS and how specific it is.
 * https://learn.microsoft.com/windows-hardware/drivers/install/inf-manufacturer-section
 * Windows uses the section with the highest OS version not higher than the running OS; the build
 * number is only compared if major.minor match exactly; product type and suite mask must match.
 * Returns null if the decoration does not apply, otherwise a sort key (higher is more specific).
 */
function windrvDecorationRank(array $models, array $os): ?array
{
    if ($models['arch'] !== $os['arch']) {
        return null;
    }
    if ($models['os_product_type'] !== null && (int)$models['os_product_type'] !== (int)($os['product_type'] ?? 0)) {
        return null;
    }
    // the suite mask of the target OS is not known
    if ($models['os_suite_mask'] !== null) {
        return null;
    }

    if ($models['os_major'] !== null) {
        $version = [(int)$models['os_major'], (int)($models['os_minor'] ?? 0)];
        $osVersion = [(int)($os['os_major'] ?? 0), (int)($os['os_minor'] ?? 0)];
        if ($version > $osVersion) {
            return null;
        }
        if ($version === $osVersion && $models['os_build'] !== null && (int)$models['os_build'] > (int)($os['os_build'] ?? 0)) {
            return null;
        }
    }

    return [
        $models['os_major'] === null ? -1 : (int)$models['os_major'],
        $models['os_minor'] === null ? -1 : (int)$models['os_minor'],
        $models['os_build'] === null ? -1 : (int)$models['os_build'],
        $models['os_product_type'] === null ? 0 : 1,
    ];
}

/**
 * Targets of the given ids that Windows would consider on the target OS: of stored drivers and of
 * the drivers of the container (WIM), only from the applicable Models section of each manufacturer.
 * $os: arch, os_major, os_minor, os_build, product_type (a container row)
 */
function windrvApplicableTargets(PDO $db, array $ids, array $os, ?string $container): array
{
    if (empty($ids)) {
        return [];
    }

    $placeholders = implode(',', array_fill(0, count($ids), '?'));
    $stmt = $db->prepare(<<<SQL
        SELECT target.id AS target, target.hwid, target.kind, target.name, target.fileset, target.models,
               target.date, target.v_major, target.v_minor, target.v_patch, target.v_build,
               driver.id AS driver, driver.container, driver.inf, driver.signed, driver.version AS driver_version,
               models.section
        FROM target
        JOIN driver ON driver.id = target.driver
        JOIN models ON models.id = target.models
        WHERE target.hwid IN ($placeholders) AND target.arch = ? AND (driver.container = '' OR driver.container = ?)
    SQL);
    $stmt->execute(array_merge($ids, [$os['arch'], $container ?? '']));
    $candidates = $stmt->fetchAll(PDO::FETCH_ASSOC);
    if (empty($candidates)) {
        return [];
    }

    // the applicable decoration of every (driver, Models section) of the candidates
    $drivers = array_values(array_unique(array_column($candidates, 'driver')));
    $placeholders = implode(',', array_fill(0, count($drivers), '?'));
    $stmt = $db->prepare("SELECT * FROM models WHERE driver IN ($placeholders)");
    $stmt->execute($drivers);
    $best = [];
    foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $models) {
        $rank = windrvDecorationRank($models, $os);
        if ($rank === null) {
            continue;
        }
        $key = $models['driver'] . '|' . $models['section'];
        if (!isset($best[$key]) || $rank > $best[$key]['rank']) {
            $best[$key] = ['rank' => $rank, 'id' => $models['id']];
        }
    }

    return array_values(array_filter($candidates, function ($t) use ($best) {
        $key = $t['driver'] . '|' . $t['section'];
        return isset($best[$key]) && $best[$key]['id'] == $t['models'];
    }));
}

/**
 * Best driver for a device: rank of the matched id (position in the device's id list),
 * then signed, then date, then version (like Windows, newer drivers win on equal rank).
 */
function windrvBest(array $targets, array $idRank): ?array
{
    usort($targets, function ($a, $b) use ($idRank) {
        $version = fn($t) => [$t['date'] ?? '', (int)$t['v_major'], (int)$t['v_minor'], (int)$t['v_patch'], (int)$t['v_build']];
        // the same id can be the hardware id of one entry and a compatible id of another
        return [$idRank[strtoupper($a['hwid'])], $a['kind'] !== 'hardware', -(int)$a['signed']]
            <=> [$idRank[strtoupper($b['hwid'])], $b['kind'] !== 'hardware', -(int)$b['signed']]
            ?: $version($b) <=> $version($a);
    });
    return $targets[0] ?? null;
}

/**
 * Driver lookup for a device (list of [id, kind] from pciDeviceIds) on a WIM image (container row):
 * - covered: the WIM contains a driver matching at least as specific as the best stored driver
 * - driver: the best stored driver (signed only unless $unsigned)
 */
function windrvMatchDevice(PDO $db, array $deviceIds, array $container, bool $unsigned = false): array
{
    $ids = array_column($deviceIds, 0);
    $idRank = array_flip(array_map('strtoupper', $ids));

    $targets = windrvApplicableTargets($db, $ids, $container, $container['guid']);
    $builtin = windrvBest(array_filter($targets, fn($t) => $t['container'] !== ''), $idRank);
    $stored = windrvBest(array_filter($targets, fn($t) => $t['container'] === '' && $t['fileset'] !== null
        && ($unsigned || $t['signed'])), $idRank);

    $covered = $builtin !== null
        && ($stored === null || $idRank[strtoupper($builtin['hwid'])] <= $idRank[strtoupper($stored['hwid'])]);

    return [
        'covered' => $builtin === null ? null : [
            'hwid' => $builtin['hwid'],
            'inf' => $builtin['inf'],
            'name' => $builtin['name'],
        ],
        'driver' => $stored === null ? null : [
            'target' => (int)$stored['target'],
            'fileset' => (int)$stored['fileset'],
            'hwid' => $stored['hwid'],
            'kind' => $stored['kind'],
            'name' => $stored['name'],
            // path of the INF inside of the downloaded package (paths are relative to driver.root)
            'inf' => $stored['inf'],
            'version' => $stored['driver_version'],
            'date' => $stored['date'],
            'signed' => (bool)$stored['signed'],
            'download' => "download.php?target=" . (int)$stored['target'],
        ],
        'inject' => !$covered && $stored !== null,
    ];
}
