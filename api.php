<?php
/*
 * windrv JSON API (used by pxe/win/menu.php and pxe/win/startnet.php)
 *
 * ?action=match&container=<WIM guid>&nics[0][ven]=8086&nics[0][dev]=1533[&nics[0][subven]=..&nics[0][subsys]=..&nics[0][rev]=..&nics[0][class]=..][&unsigned=1]
 *   driver lookup for PCI devices on the given WIM image (scanned with windrvscan.py)
 * ?action=targets&id[]=123
 *   INF path inside of the downloaded package per target
 */
header("Content-Type: application/json");

require_once 'lib.php';
$config = require 'config.php';

function respond(array $data, int $status = 200): never
{
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT) . "\n";
    exit;
}

try {
    $db = windrvOpenDb($config);
} catch (Exception $e) {
    respond(['error' => "database connection failed: " . $e->getMessage()], 500);
}

switch ($_GET['action'] ?? '') {
    case 'match':
        $guid = strtolower(trim($_GET['container'] ?? ''));
        $stmt = $db->prepare("SELECT * FROM container WHERE guid = ?");
        $stmt->execute([$guid]);
        $container = $stmt->fetch(PDO::FETCH_ASSOC);
        if (!$container) {
            respond(['error' => "unknown container '{$guid}', scan the WIM with windrvscan.py first"], 404);
        }
        if (!$container['arch']) {
            respond(['error' => "architecture of container '{$guid}' is not known, scan the WIM again"], 422);
        }

        $unsigned = ($_GET['unsigned'] ?? '') === '1';
        $results = [];
        foreach (($_GET['nics'] ?? []) as $index => $nic) {
            if (!is_array($nic)) {
                continue;
            }
            $ids = pciDeviceIds($nic['ven'] ?? '', $nic['dev'] ?? '', $nic['subven'] ?? null, $nic['subsys'] ?? null,
                                $nic['rev'] ?? null, $nic['class'] ?? null);
            if (empty($ids)) {
                $results[] = ['nic' => $index, 'error' => 'invalid vendor or device id'];
                continue;
            }
            $results[] = ['nic' => $index, 'hwid' => $ids[0][0]] + windrvMatchDevice($db, $ids, $container, $unsigned);
        }

        respond([
            'container' => [
                'guid' => $container['guid'],
                'arch' => $container['arch'],
                'version' => "{$container['os_major']}.{$container['os_minor']}.{$container['os_build']}",
            ],
            'nics' => $results,
        ]);

    case 'targets':
        $ids = array_values(array_filter(array_map('intval', (array)($_GET['id'] ?? []))));
        if (empty($ids)) {
            respond(['targets' => (object)[]]);
        }
        $placeholders = implode(',', array_fill(0, count($ids), '?'));
        $stmt = $db->prepare(<<<SQL
            SELECT target.id, target.fileset, driver.inf
            FROM target
            JOIN driver ON driver.id = target.driver
            WHERE target.id IN ($placeholders)
        SQL);
        $stmt->execute($ids);
        $targets = [];
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $row) {
            $targets[$row['id']] = [
                'inf' => $row['inf'],
                'fileset' => $row['fileset'] === null ? null : (int)$row['fileset'],
                'download' => "download.php?target={$row['id']}",
            ];
        }
        respond(['targets' => (object)$targets]);

    default:
        respond(['error' => "unknown action, expected 'match' or 'targets'"], 400);
}
