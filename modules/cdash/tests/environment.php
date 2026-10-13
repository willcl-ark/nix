<?php

require $argv[1] . '/vendor/autoload.php';

$directory = sys_get_temp_dir() . '/cdash-credentials-' . bin2hex(random_bytes(8));
mkdir($directory, 0700);
putenv("CREDENTIALS_DIRECTORY=$directory");
$secrets = [
    'APP_KEY' => 'base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    'DB_PASSWORD' => ' spaces "quotes" \\slash ${APP_KEY} #comment ',
    'MAIL_PASSWORD' => "first\nsecond\rthird",
    'AWS_ACCESS_KEY_ID' => 'example-access-key',
    'AWS_SECRET_ACCESS_KEY' => 'example/secret+key=',
];

try {
    file_put_contents("$directory/environment", "APP_KEY=old\nOTHER_SETTING=retained\n");
    foreach ($secrets as $name => $value) {
        file_put_contents("$directory/$name", "$value\n");
    }
    $argv = [__FILE__, "$directory/.env"];
    require __DIR__ . '/../scripts/write-environment.php';
    $parsed = Dotenv\Dotenv::parse(file_get_contents("$directory/.env"));
    foreach ($secrets as $name => $value) {
        if ($parsed[$name] !== $value) {
            throw new RuntimeException("Credential $name did not round-trip");
        }
    }
    if ($parsed['OTHER_SETTING'] !== 'retained') {
        throw new RuntimeException('Supplementary environment setting was lost');
    }
    if ((fileperms("$directory/.env") & 0777) !== 0600) {
        throw new RuntimeException('Environment file permissions are not private');
    }
    echo "Credential serialization checks passed\n";
} finally {
    foreach (glob("$directory/*") as $path) {
        unlink($path);
    }
    unlink("$directory/.env");
    rmdir($directory);
}
