<?php

// use dotenv if you want...
return [
    "db" => "drivers.sqlite3",

    // upload.php: python 3 to run windrvscan.py (standard library only)
    "python" => "python3",
    // upload.php: directory for uploads in progress (deleted after the import or after a day)
    "uploads" => sys_get_temp_dir() . "/windrv-uploads"
];

?>
