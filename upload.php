<?php
/*
 * Upload of a driver package folder: only the files required by the drivers are transferred.
 *
 * 1. start   POST {"files": [{"path": "folder/sub/x.inf", "size": 123}, ...]}
 *            -> {"session": "...", "infs": [...]} (manifest of the whole folder)
 * 2. file    POST ?action=file&session=..&path=..&offset=.. with the raw bytes of a chunk
 *            (INF files first, after the plan only required files)
 * 3. plan    POST ?action=plan&session=.. -> windrvscan.py --plan: all drivers of the folder with
 *            their required and missing files
 * 4. file    files of the drivers to import
 * 5. commit  POST ?action=commit&session=.. {"infs": [...]} -> windrvscan.py --store of the selected
 *            drivers, the upload is deleted
 *    cancel  POST ?action=cancel&session=..
 * Files are sent in chunks, so the php.ini upload limits (post_max_size, max_file_uploads,
 * upload_max_filesize) do not limit the size of driver files.
 */
require_once 'lib.php';
$config = require 'config.php';

const MAX_FILES = 100000;
const MAX_PATH = 1024;
const SESSION_LIFETIME = 86400;

function respond(array $data, int $status = 200): never
{
    http_response_code($status);
    header("Content-Type: application/json");
    echo json_encode($data, JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

// relative path of a file of the uploaded folder: no absolute paths, no "." or ".." components
function validPath(mixed $path): ?string
{
    if (!is_string($path) || $path === '' || strlen($path) > MAX_PATH || str_contains($path, "\0") || str_contains($path, '\\')) {
        return null;
    }
    foreach (explode('/', $path) as $part) {
        if ($part === '' || $part === '.' || $part === '..' || str_contains($part, ':')) {
            return null;
        }
    }
    return $path;
}

function isInf(string $path): bool
{
    return strtolower(pathinfo($path, PATHINFO_EXTENSION)) === 'inf';
}

function removeTree(string $dir): void
{
    if (!is_dir($dir)) {
        return;
    }
    $items = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
    foreach ($items as $item) {
        $item->isDir() && !$item->isLink() ? rmdir($item->getPathname()) : unlink($item->getPathname());
    }
    rmdir($dir);
}

function readJson(string $file): ?array
{
    $data = is_file($file) ? json_decode(file_get_contents($file), true) : null;
    return is_array($data) ? $data : null;
}

class Upload
{
    public string $dir;
    public array $state;
    // path => size of every file of the uploaded folder
    public array $manifest;

    public function __construct(public string $id, string $root)
    {
        $this->dir = $root . DIRECTORY_SEPARATOR . $id;
        $this->state = readJson($this->dir . "/state.json") ?? [];
        $this->manifest = [];
        foreach (readJson($this->dir . "/manifest.json") ?? [] as $entry) {
            $this->manifest[$entry["path"]] = $entry["size"];
        }
    }

    public function file(string $path): string
    {
        return $this->dir . "/files/" . $path;
    }

    public function complete(string $path): bool
    {
        return is_file($this->file($path)) && filesize($this->file($path)) === $this->manifest[$path];
    }

    public function saveState(): void
    {
        file_put_contents($this->dir . "/state.json", json_encode($this->state));
    }

    public function infs(): array
    {
        return array_values(array_filter(array_keys($this->manifest), fn($p) => isInf($p) && $this->complete($p)));
    }
}

function openUpload(array $config): Upload
{
    $id = $_GET['session'] ?? '';
    if (!preg_match('/^[0-9a-f]{32}$/', $id) || !is_dir($config["uploads"] . "/" . $id)) {
        respond(['error' => 'unknown upload session'], 404);
    }
    return new Upload($id, $config["uploads"]);
}

$action = $_GET['action'] ?? '';
if ($action !== '' && $_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(['error' => 'POST required'], 405);
}

switch ($action) {
    case '':
        break;

    case 'start':
        $request = json_decode(file_get_contents('php://input'), true);
        if (!is_array($request) || !is_array($request['files'] ?? null) || count($request['files']) > MAX_FILES) {
            respond(['error' => 'invalid request'], 400);
        }
        $manifest = [];
        foreach ($request['files'] as $file) {
            $path = validPath($file['path'] ?? null);
            $size = $file['size'] ?? null;
            if ($path === null || !is_int($size) || $size < 0) {
                respond(['error' => 'invalid file entry: ' . json_encode($file)], 400);
            }
            $manifest[$path] = ['path' => $path, 'size' => $size];
        }
        if (!array_filter(array_keys($manifest), 'isInf')) {
            respond(['error' => 'the folder does not contain INF files'], 400);
        }

        // uploads that were not finished within a day
        @mkdir($config["uploads"], 0770, true);
        foreach (glob($config["uploads"] . "/*", GLOB_ONLYDIR) ?: [] as $dir) {
            if (filemtime($dir) < time() - SESSION_LIFETIME) {
                removeTree($dir);
            }
        }

        $id = bin2hex(random_bytes(16));
        $dir = $config["uploads"] . "/" . $id;
        if (!mkdir($dir . "/files", 0770, true)) {
            respond(['error' => "could not create {$dir}"], 500);
        }
        file_put_contents($dir . "/manifest.json", json_encode(array_values($manifest)));
        file_put_contents($dir . "/state.json", json_encode([]));
        respond(['session' => $id, 'infs' => array_values(array_filter(array_keys($manifest), 'isInf'))]);

    case 'file':
        $upload = openUpload($config);
        $path = validPath($_GET['path'] ?? null);
        $offset = filter_var($_GET['offset'] ?? '', FILTER_VALIDATE_INT, ['options' => ['min_range' => 0]]);
        if ($path === null || $offset === false || !array_key_exists($path, $upload->manifest)) {
            respond(['error' => 'file is not part of the upload'], 400);
        }
        // before the plan only INF files, afterwards only the required files
        $allowed = isInf($path) || in_array($path, $upload->state['plan']['required'] ?? [], true);
        if (!$allowed) {
            respond(['error' => "{$path} is not required"], 403);
        }

        $data = file_get_contents('php://input');
        if ($offset + strlen($data) > $upload->manifest[$path]) {
            respond(['error' => "{$path} is larger than announced"], 400);
        }
        $target = $upload->file($path);
        if (!is_dir(dirname($target)) && !mkdir(dirname($target), 0770, true)) {
            respond(['error' => "could not create the directory of {$path}"], 500);
        }
        $handle = fopen($target, 'c');
        if (!$handle || fseek($handle, $offset) !== 0 || fwrite($handle, $data) !== strlen($data)) {
            respond(['error' => "could not write {$path}"], 500);
        }
        fclose($handle);
        clearstatcache(true, $target);
        respond(['path' => $path, 'received' => filesize($target), 'size' => $upload->manifest[$path]]);

    case 'plan':
        $upload = openUpload($config);
        $infs = $upload->infs();
        if (!$infs) {
            respond(['error' => 'no INF files uploaded'], 400);
        }
        [$code, $stdout, $stderr] = windrvScan($config, array_merge(
            ["--plan", "--manifest=" . $upload->dir . "/manifest.json", "--manifest-root=" . $upload->dir . "/files"],
            ["--"], array_map([$upload, 'file'], $infs)));
        $plan = json_decode($stdout, true);
        if ($code !== 0 || !is_array($plan)) {
            respond(['error' => "windrvscan.py --plan failed ({$code})", 'output' => $stderr], 500);
        }
        $upload->state['plan'] = $plan;
        $upload->saveState();
        $plan['uploaded'] = array_values(array_filter($plan['required'], [$upload, 'complete']));
        respond($plan);

    case 'commit':
        $upload = openUpload($config);
        $plan = $upload->state['plan'] ?? null;
        if (!$plan) {
            respond(['error' => 'plan the upload first'], 400);
        }
        // the drivers to import, chosen from the drivers of the plan
        $request = json_decode(file_get_contents('php://input'), true);
        $infs = $request['infs'] ?? null;
        if (!is_array($infs) || !$infs || count($infs) !== count(array_unique($infs, SORT_REGULAR))) {
            respond(['error' => 'select the drivers to import'], 400);
        }
        $drivers = array_column(array_filter($plan['drivers'], fn($d) => $d['skipped'] === null), null, 'inf');
        $unknown = array_values(array_filter($infs, fn($inf) => !is_string($inf) || !isset($drivers[$inf])));
        if ($unknown) {
            respond(['error' => 'not a driver of the upload', 'files' => $unknown], 400);
        }

        // only the files of the selected drivers are needed
        $required = array_unique(array_merge(...array_map(fn($inf) => $drivers[$inf]['files'], $infs)));
        $incomplete = array_values(array_filter($required, fn($p) => !$upload->complete($p)));
        if ($incomplete) {
            sort($incomplete);
            respond(['error' => 'files are not completely uploaded', 'files' => $incomplete], 400);
        }

        [$code, $stdout, $stderr] = windrvScan($config, array_merge(
            ["--database=" . windrvDbPath($config), "--store"], ["--"], array_map([$upload, 'file'], $infs)));
        removeTree($upload->dir);
        $output = str_replace($upload->dir . "/files/", '', trim($stdout . $stderr));
        if ($code !== 0) {
            respond(['error' => "windrvscan.py failed ({$code})", 'output' => $output], 500);
        }
        respond(['imported' => $infs, 'output' => $output]);

    case 'cancel':
        $upload = openUpload($config);
        removeTree($upload->dir);
        respond(['cancelled' => true]);

    default:
        respond(['error' => 'unknown action'], 400);
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upload Drivers</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        /* the hidden attribute has to win over display rules like the flex layout of .actions */
        [hidden] { display: none !important; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; padding: 20px; background: #f5f5f5; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; }
        h1 { color: #333; }
        h2 { font-size: 16px; color: #555; margin: 25px 0 10px; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .muted { color: #888; }
        .small { font-size: 12px; }
        .code { font-family: 'Courier New', monospace; font-size: 13px; }

        .drop { border: 2px dashed #b6c2d1; border-radius: 8px; padding: 40px 20px; text-align: center; cursor: pointer; transition: background-color .15s, border-color .15s; }
        .drop:hover, .drop:focus { border-color: #007bff; background: #f7fbff; outline: none; }
        .drop.dragover { border-color: #007bff; background: #eaf3ff; }
        .drop.busy { cursor: progress; opacity: .7; }
        .drop svg { width: 48px; height: 48px; color: #007bff; margin-bottom: 10px; }
        .drop-title { font-size: 18px; font-weight: 600; }
        .drop-hint { color: #888; margin-top: 5px; }
        .drop-info { margin-top: 12px; font-weight: 500; color: #555; }
        .drop, .drop-title, .drop-hint, .drop-info { display: block; }
        .drop svg { display: inline-block; }
        /* hidden but still part of the layout: some browsers do not open the picker of display:none inputs */
        .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

        #status { margin-top: 20px; }
        .notice { padding: 12px 16px; border-radius: 6px; margin-bottom: 15px; }
        .notice.info { background: #f1f5f9; }
        .notice.error { background: #f8d7da; color: #721c24; }
        .notice.warning { background: #fff3cd; color: #856404; }
        .notice.success { background: #d4edda; color: #155724; }
        .notice pre { margin-top: 8px; }
        .progress-label { margin-bottom: 6px; }
        progress { width: 100%; height: 14px; }

        .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 25px; }
        .card { background: #f8f9fa; border-radius: 6px; padding: 14px 16px; }
        .card-value { font-size: 24px; font-weight: 600; }
        .card-label { color: #777; font-size: 13px; margin-top: 2px; }
        .card.highlight { background: #eaf3ff; }
        .card.highlight .card-value { color: #007bff; }
        .card.warning { background: #fff3cd; color: #856404; }

        .chips { display: flex; flex-wrap: wrap; gap: 8px; }
        .chip { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border: 1px solid #ddd; border-radius: 20px; cursor: pointer; user-select: none; font-size: 14px; }
        .chip:hover { border-color: #007bff; }
        .chip.selected { border-color: #007bff; background: #eaf3ff; }
        .chip input { accent-color: #007bff; }
        .chip .count { color: #888; font-size: 12px; }

        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; vertical-align: top; }
        th { background-color: #f8f9fa; font-weight: 600; color: #555; }
        .select-col { width: 32px; }
        tr.unselected td { color: #aaa; }
        tr.unselected .badge { opacity: .6; }
        .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
        .badge.ok { background: #d4edda; color: #155724; }
        .badge.warning { background: #fff3cd; color: #856404; }
        details { margin-top: 15px; }
        summary { cursor: pointer; color: #555; }
        pre { background: #f4f4f4; padding: 10px; border-radius: 4px; font-size: 12px; max-height: 300px; overflow: auto; white-space: pre-wrap; }

        .actions { position: sticky; bottom: 0; display: flex; gap: 10px; align-items: center; justify-content: flex-end; flex-wrap: wrap;
                   margin: 25px -30px -30px; padding: 16px 30px; background: white; border-top: 1px solid #eee; border-radius: 0 0 8px 8px;
                   box-shadow: 0 -2px 6px rgba(0,0,0,0.05); }
        .actions #selection { margin-right: auto; color: #555; }
        button { padding: 9px 20px; background-color: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        button:hover { background-color: #0056b3; }
        button:disabled { background-color: #9cc3ee; cursor: not-allowed; }
        button.secondary { background-color: #6c757d; }
        button.secondary:hover { background-color: #545b62; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Upload Drivers</h1>
            <a href="index.php">Driver database</a>
        </header>

        <!-- a label opens the folder picker natively, also without JavaScript -->
        <label id="drop" class="drop" for="picker" tabindex="0">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
                <path d="M12 10v6m-3-3 3-3 3 3"/>
            </svg>
            <span class="drop-title">Drop a driver folder here</span>
            <span class="drop-hint">or click to select the extracted driver package - only the files the drivers need are uploaded</span>
            <span id="dropInfo" class="drop-info"></span>
        </label>
        <input type="file" id="picker" class="visually-hidden" webkitdirectory multiple>

        <div id="status"></div>
        <div id="review" hidden></div>

        <div id="actions" class="actions" hidden>
            <span id="selection"></span>
            <button id="cancel" class="secondary">Cancel</button>
            <button id="import">Upload and import</button>
        </div>
    </div>

    <script>window.WINDRV_CLASSES = <?php echo json_encode(WINDRV_DEVICE_CLASSES, JSON_HEX_TAG | JSON_HEX_AMP); ?>;</script>
    <!-- the modification time in the URL makes browsers load a changed upload.js instead of a cached one -->
    <script src="upload.js?v=<?php echo @filemtime(__DIR__ . '/upload.js') ?: 0; ?>" onerror="document.getElementById('status').innerHTML = '<div class=&quot;notice error&quot;>upload.js could not be loaded - copy it next to upload.php on the server.</div>'"></script>
</body>
</html>
