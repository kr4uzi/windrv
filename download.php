<?php

if (!isset($_GET['target'])) {
    die("target is required");
}

require_once 'lib.php';
$config = require 'config.php';

try {
    $db = windrvOpenDb($config);
} catch (Exception $e) {
    die("Database connection failed: " . $e->getMessage());
}

$stmt = $db->prepare(<<<SQL
    SELECT driver.root, driver.inf, driver.version, target.arch, target.fileset
    FROM `target`
    JOIN driver ON driver.id = `target`.driver
    WHERE `target`.id = ?
SQL);
$stmt->execute([$_GET['target']]);
$target = $stmt->fetch(PDO::FETCH_ASSOC);
if (!$target) {
    die("target unknown");
}
if ($target['fileset'] === null) {
    // built-in driver of a WIM (container), its files are not stored
    die("target has no files");
}

// driver.root is relative to the database directory unless it is absolute
$root = $target['root'];
if (!str_starts_with($root, '/')) {
    $root = dirname(windrvDbPath($config)) . DIRECTORY_SEPARATOR . $root;
}
$root = realpath($root);
if (!$root) {
    die("driver package not found");
}

$stmt = $db->prepare(<<<SQL
    SELECT `path`
    FROM fileset_file
    WHERE fileset = ?
    ORDER BY `path`
SQL);
$stmt->execute([$target['fileset']]);
// <inf>-<version>-<arch>, e.g. e1r68x64-12.19.2.60-amd64
$name = preg_replace('/[^A-Za-z0-9._-]/', '_',
    implode('-', array_filter([pathinfo($target['inf'], PATHINFO_FILENAME), $target['version'], $target['arch']])));

$paths = array();
foreach ($stmt->fetchAll(PDO::FETCH_COLUMN) as $path) {
    $file_path = realpath($root . DIRECTORY_SEPARATOR . $path);
    // only files inside of the driver package
    if ($file_path && is_file($file_path) && str_starts_with($file_path, $root . DIRECTORY_SEPARATOR)) {
        $paths[] = $path;
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
    header('Content-Disposition: attachment; filename="' . $name . '.zip"');
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
    header('Content-Disposition: attachment; filename="' . $name . '.cab"');
    header('Content-Length: ' . filesize($cabFile));
    flush();
    readfile($cabFile);
    unlink($cabFile);
}
?>
