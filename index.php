<?php
$config = require 'config.php';

try {
    $db = new PDO("sqlite:" . $config["db"], null, null, [PDO::SQLITE_ATTR_OPEN_FLAGS => PDO::SQLITE_OPEN_READONLY]);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
} catch (Exception $e) {
    die("Database connection failed: " . $e->getMessage());
}

// Get search parameter
$hwid_search = isset($_GET['hwid']) ? $_GET['hwid'] : '';

// Pagination settings
$per_page = 50;
$current_page = isset($_GET['page']) ? max(1, intval($_GET['page'])) : 1;
$offset = ($current_page - 1) * $per_page;

// Build WHERE clause
$where_clause = "";
$params = [];

if (!empty($hwid_search)) {
    $where_clause = " WHERE hwid LIKE ?";
    $params[] = '%' . $hwid_search . '%';
}

// Get total count
$count_sql = "SELECT COUNT(*) FROM target" . $where_clause;
$count_stmt = $db->prepare($count_sql);
$count_stmt->execute($params);
$total_results = $count_stmt->fetchColumn();
$total_pages = ceil($total_results / $per_page);

// Build main query with pagination
$sql = "SELECT
    target.id,
    target.hwid,
    target.v_major,
    target.v_minor,
    target.v_patch,
    target.v_build,
    target.arch,
    target.os_major,
    target.os_minor,
    target.os_build,
    target.date,
    driver.inf,
    driver.container
FROM target
JOIN driver ON driver.id = target.driver" . $where_clause . " ORDER BY target.date DESC, target.id DESC LIMIT ? OFFSET ?";

$params[] = $per_page;
$params[] = $offset;

$stmt = $db->prepare($sql);
$stmt->execute($params);
$results = $stmt->fetchAll(PDO::FETCH_ASSOC);
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
                        <th>Hardware ID</th>
                        <th>Windows</th>
                        <th>Version</th>
                        <th>File</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    <tr class="filter-row">
                        <td>
                            <input type="text" name="hwid" value="<?php echo htmlspecialchars($hwid_search); ?>" placeholder="Search Hardware ID...">
                        </td>
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td>
                            <input type="text" disabled placeholder="Filter disabled">
                        </td>
                        <td>
                            <button type="submit">Search</button>
                            <?php if (!empty($hwid_search)): ?>
                                <a href="index.php" class="clear-btn" style="display: inline-block; padding: 8px 20px; text-decoration: none; color: white; border-radius: 4px;">Clear</a>
                            <?php endif; ?>
                        </td>
                    </tr>
                    <?php if (empty($results)): ?>
                        <tr>
                            <td colspan="5" style="text-align: center; padding: 40px; color: #999;">
                                No drivers found. <?php echo !empty($hwid_search) ? 'Try a different search term.' : 'Database is empty.'; ?>
                            </td>
                        </tr>
                    <?php else: ?>
                        <?php foreach ($results as $row): ?>
                            <tr>
                                <td>
                                    <span class="code"><?php echo htmlspecialchars($row['hwid']); ?></span>
                                </td>
                                <td>
                                    <?php
                                        $arch = htmlspecialchars($row['arch'] ?? '');
                                        $os_parts = array_filter([
                                            $row['os_major'],
                                            $row['os_minor'],
                                            $row['os_build']
                                        ], function($v) { return !is_null($v); });
                                        $os_version = !empty($os_parts) ? implode('.', $os_parts) : '';
                                        echo $os_version;
                                        if ($os_version) echo ' ';
                                        if ($arch) echo '(' . $arch . ')';
                                    ?>
                                </td>
                                <td>
                                    <?php
                                        $version_parts = array_filter([
                                            $row['v_major'],
                                            $row['v_minor'],
                                            $row['v_patch'],
                                            $row['v_build']
                                        ], function($v) { return !is_null($v); });

                                        echo !empty($version_parts) ? implode('.', $version_parts) : '';
                                        if ($version_parts) echo ' ';

                                        if ($row['date']) {
                                            // Assuming date is stored as Unix timestamp
                                            echo '(' . date('Y-m-d', $row['date']) . ')';
                                        }
                                    ?>
                                </td>
                                <td>
                                    <?php
                                        if (!empty($row['inf'])) {
                                            echo '<span class="code">' . htmlspecialchars($row['inf']) . '</span>';
                                        } else {
                                            echo '<span style="color: #ccc;">N/A</span>';
                                        }
                                    ?>
                                </td>
                                <td>
                                    <?php
                                        if (!preg_match('/^[a-zA-Z0-9]{32}$/', $row['container'] ?? '')) {
                                            echo '<a href="download.php?target=' . $row['id'] . '&as=zip" class="download-link">Download</a>';
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
