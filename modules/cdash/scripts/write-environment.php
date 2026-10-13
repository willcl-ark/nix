<?php

// systemd exposes only the credentials selected by the module here.
$directory = getenv('CREDENTIALS_DIRECTORY');
$environment = '';
if (is_file("$directory/environment")) {
    $environment = file_get_contents("$directory/environment");
    if ($environment === false) {
        throw new RuntimeException('Cannot read supplementary environment file');
    }
    $environment .= "\n";
}

foreach (['APP_KEY', 'DB_PASSWORD', 'MAIL_PASSWORD', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'] as $name) {
    $path = "$directory/$name";
    if (!is_file($path)) {
        continue;
    }

    $value = file_get_contents($path);
    if ($value === false) {
        throw new RuntimeException("Cannot read credential $name");
    }
    // Strip the final newline commonly added to secret files, preserving spaces.
    $value = rtrim($value, "\r\n");
    // dotenv double quotes require escaping dollars to prevent interpolation.
    $value = strtr($value, [
        '\\' => '\\\\',
        '"' => '\\"',
        '$' => '\\$',
        "\r" => '\\r',
        "\n" => '\\n',
    ]);
    $environment .= "$name=\"$value\"\n";
}

umask(0077);
if (file_put_contents($argv[1], $environment) === false) {
    throw new RuntimeException('Cannot write CDash environment file');
}
