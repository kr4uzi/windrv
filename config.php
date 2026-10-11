<?php

// use dotenv if you want...
return [
    "db" => "drivers.sqlite3",

    // upload.php: python of the venv with the windrvscan.py requirements (see README.md)
    "python" => "/srv/windrv/.venv/bin/python",
    // upload.php: directory for uploads in progress (deleted after the import or after a day)
    "uploads" => sys_get_temp_dir() . "/windrv-uploads"
];

?>
