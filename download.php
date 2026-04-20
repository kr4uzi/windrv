<?php

if (!isset($_GET['target'])) {
    die("target is required");
}

$config = require 'config.php';

try {
    $db = new PDO("sqlite:" . $config["db"], null, null, [PDO::SQLITE_ATTR_OPEN_FLAGS => PDO::SQLITE_OPEN_READONLY]);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
} catch (Exception $e) {
    die("Database connection failed. {$e}");
}

$stmt = $db->prepare(<<<SQL
    SELECT driver.root as driver_root, target.root as target_root, target.id as target_id
    FROM `target`
    JOIN driver ON driver.id = `target`.driver
    WHERE `target`.id = ?
SQL);
$stmt->execute([$_GET['target']]);
$driver = $stmt->fetch(PDO::FETCH_ASSOC);
if (!$driver) {
    die("target unknown");
}

$root = $driver['driver_root'] . DIRECTORY_SEPARATOR . $driver['target_root'];    
$stmt = $db->prepare(<<<SQL
    SELECT `path`
    FROM `file`
    WHERE `target` = ?
SQL);
$stmt->execute([$driver['target_id']]);
$files = $stmt->fetchAll(PDO::FETCH_ASSOC);
$paths = array();
foreach ($files as $file) {
    $file_path = $root . DIRECTORY_SEPARATOR . $file['path'];
    if (file_exists($file_path)) {
        $paths[] = $file['path'];
    }
}

if (isset($_GET['as']) && $_GET['as'] === 'zip') {
    $zipFile = tempnam(sys_get_temp_dir(), 'windrv_') . '.zip';
    $zip = new ZipArchive();
    if ($zip->open($zipFile, ZipArchive::CREATE) !== TRUE) {
        die("Failed to create ZIP");
    }
    foreach ($paths as $path) {
        $fullPath = $root . DIRECTORY_SEPARATOR . $path;
        $zip->addFile($fullPath, $path);
    }
    $zip->close();
    header('Content-Type: application/zip');
    header('Content-Disposition: attachment; filename="driver.zip"');
    header('Content-Length: ' . filesize($zipFile));
    flush();
    readfile($zipFile);
    unlink($zipFile);
} else {
    $cabFile = tempnam(sys_get_temp_dir(), 'windrv_') . '.cab';
    $cmd = 'cd ' . escapeshellarg($root) . ' && gcab -c ' .
           escapeshellarg($cabFile) . ' ' .
           implode(' ', array_map('escapeshellarg', $paths));

    exec($cmd, $output, $returnCode);

    if ($returnCode !== 0) {
        unlink($cabFile);
        die("Failed to create CAB");
    }

    header('Content-Type: application/vnd.ms-cab-compressed');
    header('Content-Disposition: attachment; filename="driver.cab"');
    header('Content-Length: ' . filesize($cabFile));
    flush();
    readfile($cabFile);
    unlink($cabFile);
}
?>