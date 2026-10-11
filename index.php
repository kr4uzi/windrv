<?php
require_once 'lib.php';
$config = require 'config.php';

try {
    $db = windrvOpenDb($config);
} catch (WindrvDatabaseMissing $e) {
    // the database is created by windrvscan.py, offer to do that (or show the command)
    $error = $output = null;
    if (($_GET['action'] ?? '') === 'init' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        [$code, $stdout, $stderr] = windrvScan($config, ["--database=" . windrvDbPath($config), "--init"]);
        if ($code === 0) {
            header('Location: index.php', true, 303);
            exit;
        }
        $error = "windrvscan.py --init failed (exit code {$code})";
        $output = trim($stdout . "\n" . $stderr);
    }
    $path = htmlspecialchars(windrvDbPath($config));
    $init = htmlspecialchars(windrvScanCommand($config, ["--init"]));
    $scan = htmlspecialchars(windrvScanCommand($config, ["--class-filter=Net", "/path/to/boot.wim"]));
    $failure = $error ? '<div class="error">' . htmlspecialchars($error) . '<pre>' . htmlspecialchars($output) . '</pre></div>' : '';
    http_response_code(503);
    echo <<<HTML
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Driver Database</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; padding: 20px; background: #f5f5f5; }
            .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h1 { margin: 0 0 20px; color: #333; }
            p { margin: 10px 0; }
            pre { background: #f4f4f4; padding: 10px; border-radius: 4px; font-size: 13px; white-space: pre-wrap; word-break: break-all; }
            button { padding: 8px 20px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
            .error { background: #f8d7da; color: #721c24; padding: 10px 14px; border-radius: 4px; margin: 15px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Driver Database</h1>
            <p>The database <code>{$path}</code> does not exist yet. It is created by <code>windrvscan.py</code>,
               either empty or by the first scan, for example of the boot.wim:</p>
            <pre>{$init}</pre>
            <pre>{$scan}</pre>
            <p>The web server user needs write access to the database directory to create it from here
               (see README.md).</p>
            {$failure}
            <form method="POST" action="index.php?action=init"><button type="submit">Create empty database</button></form>
        </div>
    </body>
    </html>
    HTML;
    exit;
} catch (Exception $e) {
    die("Database connection failed: " . $e->getMessage());
}

// search parameters
$hwid_search = trim($_GET['hwid'] ?? '');
$arch_filter = $_GET['arch'] ?? '';
$architectures = $db->query("SELECT DISTINCT arch FROM target ORDER BY arch")->fetchAll(PDO::FETCH_COLUMN);
if (!in_array($arch_filter, $architectures, true)) {
    $arch_filter = '';
}

// Pagination settings
$per_page = 50;
$current_page = isset($_GET['page']) ? max(1, intval($_GET['page'])) : 1;
$offset = ($current_page - 1) * $per_page;

// Build WHERE clause
$conditions = [];
$params = [];
$rank = "0";
$rank_params = [];

// a full PCI device id (as shown by the device manager) is expanded to the ids Windows matches,
// otherwise the search is a fragment of the hardware id (e.g. DEV_1533)
$device = $hwid_search !== '' ? parsePciDeviceId($hwid_search) : null;
$device_ids = [];
if ($device) {
    $device_ids = array_column(pciDeviceIds($device['ven'], $device['dev'], $device['subven'], $device['subsys'], $device['rev'], $device['class']), 0);
    $conditions[] = "target.hwid IN (" . implode(',', array_fill(0, count($device_ids), '?')) . ")";
    $params = array_merge($params, $device_ids);
    // rank of the best matching id (position in the device's id list)
    $rank = "MIN(CASE target.hwid " . str_repeat("WHEN ? THEN ? ", count($device_ids)) . "END)";
    foreach ($device_ids as $position => $id) {
        array_push($rank_params, $id, $position);
    }
} elseif ($hwid_search !== '') {
    $conditions[] = "target.hwid LIKE ? ESCAPE '\\'";
    $params[] = '%' . addcslashes($hwid_search, '%_\\') . '%';
}
if ($arch_filter !== '') {
    $conditions[] = "target.arch = ?";
    $params[] = $arch_filter;
}
$where_clause = $conditions ? " WHERE " . implode(" AND ", $conditions) : "";

// one row per driver package, architecture and file set (targets with the same files)
$group_by = " GROUP BY driver.id, target.arch, target.fileset";

// Get total count
$count_stmt = $db->prepare("SELECT COUNT(*) FROM (SELECT 1 FROM target JOIN driver ON driver.id = target.driver" . $where_clause . $group_by . ")");
$count_stmt->execute($params);
$total_results = $count_stmt->fetchColumn();
$total_pages = ceil($total_results / $per_page);

// Build main query with pagination
$sql = "SELECT
    MIN(target.id) AS id,
    -- all (matching) ids of the row: hwid, kind and name separated by tabs, one per line
    GROUP_CONCAT(target.hwid || char(9) || target.kind || char(9) || IFNULL(target.name, ''), char(10)) AS ids,
    SUM(target.kind = 'hardware') AS hardware,
    target.arch,
    target.fileset,
    GROUP_CONCAT(DISTINCT NULLIF(models.decoration, '')) AS decorations,
    MAX(target.date) AS date,
    driver.version,
    driver.inf,
    driver.container,
    driver.signed,
    (SELECT SUM(size) FROM fileset_file WHERE fileset_file.fileset = target.fileset) AS size,
    $rank AS rank
FROM target
JOIN driver ON driver.id = target.driver
JOIN models ON models.id = target.models" . $where_clause . $group_by . "
ORDER BY rank, date DESC, driver.id DESC LIMIT ? OFFSET ?";

$stmt = $db->prepare($sql);
$stmt->execute(array_merge($rank_params, $params, [$per_page, $offset]));
$results = $stmt->fetchAll(PDO::FETCH_ASSOC);

// device ids of a row, without duplicates (an id can be listed for several target OS versions):
// the best match of a searched device id first, then hardware before compatible ids
function row_ids(array $row, array $device_ids): array
{
    $ids = [];
    foreach (explode("\n", $row['ids']) as $line) {
        [$hwid, $kind, $name] = array_pad(explode("\t", $line, 3), 3, '');
        $key = strtoupper($hwid);
        if (!isset($ids[$key]) || $kind === 'hardware') {
            $ids[$key] = ['hwid' => $hwid, 'kind' => $kind, 'name' => $name];
        }
    }
    $rank = array_flip(array_map('strtoupper', $device_ids));
    uasort($ids, fn($a, $b) =>
        [$rank[strtoupper($a['hwid'])] ?? PHP_INT_MAX, $a['kind'] !== 'hardware', $a['hwid']]
        <=> [$rank[strtoupper($b['hwid'])] ?? PHP_INT_MAX, $b['kind'] !== 'hardware', $b['hwid']]);
    return array_values($ids);
}

// expanded lists are limited, GPU drivers have thousands of ids with the same files
const MAX_LISTED_IDS = 200;

// tooltip of the "compatible" badge
const COMPATIBLE_ID_TITLE = "Compatible ID: the INF lists this id after the hardware id of a device entry (or without a hardware id). "
    . "A compatible id matches a whole family of devices, Windows prefers drivers that match a hardware id of the device.";

function format_size(?int $bytes): string
{
    if ($bytes === null) return '';
    foreach (['B', 'KB', 'MB', 'GB'] as $unit) {
        if ($bytes < 1024 || $unit === 'GB') return ($unit === 'B' ? $bytes : number_format($bytes, 1)) . " $unit";
        $bytes /= 1024;
    }
    return '';
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Driver Database</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            padding: 20px;
            background: #f5f5f5;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        h1 {
            margin-bottom: 20px;
            color: #333;
        }

        .filter-form {
            margin-bottom: 20px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }

        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }

        th {
            background-color: #f8f9fa;
            font-weight: 600;
            color: #555;
        }

        tr:hover {
            background-color: #f8f9fa;
        }

        input[type="text"] {
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            width: 100%;
        }

        input[type="text"]:disabled {
            background-color: #f0f0f0;
            cursor: not-allowed;
        }

        button {
            padding: 8px 20px;
            background-color: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            margin-right: 10px;
        }

        button:hover {
            background-color: #0056b3;
        }

        .clear-btn {
            background-color: #6c757d;
        }

        .clear-btn:hover {
            background-color: #545b62;
        }

        .filter-row td {
            padding-top: 5px;
            padding-bottom: 15px;
        }

        .actions {
            margin-bottom: 15px;
        }

        .result-count {
            color: #666;
            font-size: 14px;
            margin-top: 10px;
        }

        .download-link {
            color: #007bff;
            text-decoration: none;
        }

        .download-link:hover {
            text-decoration: underline;
        }

        .code {
            font-family: 'Courier New', monospace;
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 13px;
            cursor: pointer;
        }

        details.more {
            margin-top: 4px;
        }

        details.more summary {
            display: inline-block;
            padding: 1px 6px;
            border-radius: 3px;
            background: #e9ecef;
            color: #555;
            font-size: 12px;
            cursor: pointer;
        }

        details.more ul {
            list-style: none;
            margin-top: 6px;
            max-height: 300px;
            overflow: auto;
        }

        details.more li {
            margin: 3px 0;
            font-size: 13px;
        }

        .muted {
            color: #888;
        }

        .badge[title] {
            cursor: help;
        }

        .badge {
            display: inline-block;
            padding: 1px 6px;
            margin-left: 4px;
            border-radius: 3px;
            background: #e9ecef;
            color: #555;
            font-size: 12px;
        }

        .badge.warning {
            background: #fff3cd;
            color: #856404;
        }

        select {
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            width: 100%;
        }

        .pagination {
            display: flex;
            justify-content: center;
            align-items: center;
            margin-top: 30px;
            gap: 5px;
        }

        .pagination a,
        .pagination span {
            padding: 8px 12px;
            border: 1px solid #ddd;
            background-color: white;
            color: #007bff;
            text-decoration: none;
            border-radius: 4px;
            font-size: 14px;
        }

        .pagination a:hover {
            background-color: #007bff;
            color: white;
        }

        .pagination .current {
            background-color: #007bff;
            color: white;
            border-color: #007bff;
        }

        .pagination .disabled {
            color: #ccc;
            cursor: not-allowed;
            border-color: #ddd;
        }

        .pagination .disabled:hover {
            background-color: white;
            color: #ccc;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Driver Database</h1>
        <p style="margin-bottom: 15px;"><a href="upload.php" class="download-link">Upload drivers</a></p>

        <form method="GET" class="filter-form">
            <div class="result-count">
                <?php if ($total_results > 0): ?>
                    Showing <?php echo $offset + 1; ?> - <?php echo min($offset + $per_page, $total_results); ?> of <?php echo $total_results; ?> driver(s)
                <?php else: ?>
                    Found 0 drivers
                <?php endif; ?>
            </div>

            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Hardware ID</th>
                        <th>Windows</th>
                        <th>Version</th>
                        <th>File</th>
                        <th>Size</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    <tr class="filter-row">
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td>
                            <input type="text" name="hwid" value="<?php echo htmlspecialchars($hwid_search); ?>" placeholder="DEV_1533 or PCI\VEN_..&DEV_..&SUBSYS_..">
                        </td>
                        <td>
                            <select name="arch" onchange="this.form.submit()">
                                <option value="">All architectures</option>
                                <?php foreach ($architectures as $arch): ?>
                                    <option value="<?php echo htmlspecialchars($arch); ?>"<?php echo $arch === $arch_filter ? ' selected' : ''; ?>><?php echo htmlspecialchars($arch); ?></option>
                                <?php endforeach; ?>
                            </select>
                        </td>
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td></td>
                        <td>
                            <button type="submit">Search</button>
                            <?php if (!empty($hwid_search) || $arch_filter !== ''): ?>
                                <a href="index.php" class="clear-btn" style="display: inline-block; padding: 8px 20px; text-decoration: none; color: white; border-radius: 4px;">Clear</a>
                            <?php endif; ?>
                        </td>
                    </tr>
                    <?php if (empty($results)): ?>
                        <tr>
                            <td colspan="7" style="text-align: center; padding: 40px; color: #999;">
                                No drivers found. <?php echo !empty($hwid_search) ? 'Try a different search term.' : 'Database is empty.'; ?>
                            </td>
                        </tr>
                    <?php else: ?>
                        <?php foreach ($results as $row): ?>
                            <tr>
                                <?php
                                    $ids = row_ids($row, $device_ids);
                                    $first = $ids[0];
                                ?>
                                <td>
                                    <?php echo htmlspecialchars($first['name']); ?>
                                </td>
                                <td>
                                    <span class="code"><?php echo htmlspecialchars($first['hwid']); ?></span>
                                    <?php if (count($ids) > 1): ?>
                                        <details class="more">
                                            <summary title="device ids with the same driver files">+<?php echo count($ids) - 1; ?> more</summary>
                                            <ul>
                                                <?php foreach (array_slice($ids, 1, MAX_LISTED_IDS) as $id): ?>
                                                    <li>
                                                        <span class="code"><?php echo htmlspecialchars($id['hwid']); ?></span>
                                                        <?php if ($id['kind'] !== 'hardware'): ?><span class="badge" title="<?php echo htmlspecialchars(COMPATIBLE_ID_TITLE); ?>">compatible</span><?php endif; ?>
                                                        <?php if ($id['name'] !== $first['name']): ?><span class="muted"><?php echo htmlspecialchars($id['name']); ?></span><?php endif; ?>
                                                    </li>
                                                <?php endforeach; ?>
                                                <?php if (count($ids) - 1 > MAX_LISTED_IDS): ?>
                                                    <li class="muted">... and <?php echo count($ids) - 1 - MAX_LISTED_IDS; ?> more, search to narrow the list</li>
                                                <?php endif; ?>
                                            </ul>
                                        </details>
                                    <?php endif; ?>
                                    <?php if (!$row['hardware']): ?>
                                        <span class="badge" title="<?php echo htmlspecialchars("All ids of this row are compatible ids. " . COMPATIBLE_ID_TITLE); ?>">compatible</span>
                                    <?php endif; ?>
                                </td>
                                <td>
                                    <?php
                                        echo htmlspecialchars($row['arch']);
                                        if ($row['decorations']) {
                                            echo ' <span class="badge" title="TargetOSVersion">' . htmlspecialchars(str_replace(',', ', ', $row['decorations'])) . '</span>';
                                        }
                                    ?>
                                </td>
                                <td>
                                    <?php
                                        echo htmlspecialchars($row['version'] ?? '');
                                        if ($row['date']) {
                                            // stored as YYYY-MM-DD
                                            echo ' (' . htmlspecialchars($row['date']) . ')';
                                        }
                                    ?>
                                </td>
                                <td>
                                    <span class="code"><?php echo htmlspecialchars($row['inf']); ?></span>
                                    <?php if ($row['container'] !== ''): ?>
                                        <span class="badge" title="built-in driver of WIM <?php echo htmlspecialchars($row['container']); ?>">WIM</span>
                                    <?php elseif (!$row['signed']): ?>
                                        <span class="badge warning" title="no CatalogFile, 64-bit WinPE does not load unsigned drivers">unsigned</span>
                                    <?php endif; ?>
                                </td>
                                <td>
                                    <?php echo format_size($row['size'] === null ? null : (int)$row['size']); ?>
                                </td>
                                <td>
                                    <?php
                                        // built-in drivers of a WIM (container) have no files
                                        if ($row['fileset'] !== null) {
                                            echo '<a href="download.php?target=' . $row['id'] . '&as=zip" class="download-link">ZIP</a> ';
                                            echo '<a href="download.php?target=' . $row['id'] . '" class="download-link">CAB</a>';
                                        } else {
                                            echo '<span style="color: #ccc; cursor: not-allowed;">Download</span>';
                                        }
                                    ?>
                                </td>
                            </tr>
                        <?php endforeach; ?>
                    <?php endif; ?>
                </tbody>
            </table>
        </form>

        <?php if ($total_pages > 1): ?>
            <div class="pagination">
                <?php
                // Build query string for pagination links
                $query_params = [];
                if (!empty($hwid_search)) {
                    $query_params['hwid'] = $hwid_search;
                }
                if ($arch_filter !== '') {
                    $query_params['arch'] = $arch_filter;
                }

                function build_pagination_url($page, $query_params) {
                    $query_params['page'] = $page;
                    return 'index.php?' . http_build_query($query_params);
                }

                // Previous button
                if ($current_page > 1): ?>
                    <a href="<?php echo build_pagination_url($current_page - 1, $query_params); ?>">← Previous</a>
                <?php else: ?>
                    <span class="disabled">← Previous</span>
                <?php endif; ?>

                <?php
                // Page numbers
                $range = 2; // Show 2 pages before and after current page
                $start = max(1, $current_page - $range);
                $end = min($total_pages, $current_page + $range);

                // First page
                if ($start > 1): ?>
                    <a href="<?php echo build_pagination_url(1, $query_params); ?>">1</a>
                    <?php if ($start > 2): ?>
                        <span>...</span>
                    <?php endif; ?>
                <?php endif; ?>

                <?php
                // Page range
                for ($i = $start; $i <= $end; $i++):
                    if ($i == $current_page): ?>
                        <span class="current"><?php echo $i; ?></span>
                    <?php else: ?>
                        <a href="<?php echo build_pagination_url($i, $query_params); ?>"><?php echo $i; ?></a>
                    <?php endif; ?>
                <?php endfor; ?>

                <?php
                // Last page
                if ($end < $total_pages): ?>
                    <?php if ($end < $total_pages - 1): ?>
                        <span>...</span>
                    <?php endif; ?>
                    <a href="<?php echo build_pagination_url($total_pages, $query_params); ?>"><?php echo $total_pages; ?></a>
                <?php endif; ?>

                <?php
                // Next button
                if ($current_page < $total_pages): ?>
                    <a href="<?php echo build_pagination_url($current_page + 1, $query_params); ?>">Next →</a>
                <?php else: ?>
                    <span class="disabled">Next →</span>
                <?php endif; ?>
            </div>
        <?php endif; ?>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const codeElements = document.querySelectorAll('.code');
            codeElements.forEach(function(el) {
                el.addEventListener('dblclick', function() {
                    const range = document.createRange();
                    range.selectNodeContents(this);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                });
            });
        });
    </script>
</body>
</html>
