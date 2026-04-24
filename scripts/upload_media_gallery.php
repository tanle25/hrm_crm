<?php

$image_urls_b64 = getenv('IMAGE_URLS_B64') ?: '';
$image_alt = getenv('IMAGE_ALT') ?: '';

if (!$image_urls_b64) {
    fwrite(STDERR, "Missing IMAGE_URLS_B64\n");
    exit(1);
}

require_once ABSPATH . 'wp-admin/includes/media.php';
require_once ABSPATH . 'wp-admin/includes/file.php';
require_once ABSPATH . 'wp-admin/includes/image.php';

function cf_guess_extension_from_file($tmp_path)
{
    if (!function_exists('wp_get_image_mime')) {
        return 'jpg';
    }

    $mime = wp_get_image_mime($tmp_path);
    $map = array(
        'image/jpeg' => 'jpg',
        'image/png' => 'png',
        'image/gif' => 'gif',
        'image/webp' => 'webp',
        'image/bmp' => 'bmp',
        'image/tiff' => 'tiff',
        'image/avif' => 'avif',
    );

    return isset($map[$mime]) ? $map[$mime] : 'jpg';
}

function cf_sideload_image_without_extension($url, $image_alt, $index)
{
    $tmp = download_url($url, 30);
    if (is_wp_error($tmp)) {
        return $tmp;
    }

    $extension = cf_guess_extension_from_file($tmp);
    $filename = 'source-image-' . md5($url) . '.' . $extension;
    $file_array = array(
        'name' => $filename,
        'tmp_name' => $tmp,
    );

    $attachment_id = media_handle_sideload($file_array, 0);
    if (is_wp_error($attachment_id)) {
        @unlink($tmp);
        return $attachment_id;
    }

    if ($image_alt) {
        update_post_meta($attachment_id, '_wp_attachment_image_alt', $image_alt . ' - hình ' . ($index + 1));
    }

    return array(
        'source_url' => $url,
        'id' => intval($attachment_id),
        'url' => wp_get_attachment_url($attachment_id),
        'alt' => get_post_meta($attachment_id, '_wp_attachment_image_alt', true),
    );
}

$decoded = json_decode(base64_decode($image_urls_b64), true);
if (!is_array($decoded)) {
    fwrite(STDERR, "Invalid image payload\n");
    exit(1);
}

$uploaded = array();
$errors = array();
foreach (array_slice(array_unique(array_filter($decoded)), 0, 5) as $index => $url) {
    $attachment_id = media_sideload_image($url, 0, null, 'id');
    if (is_wp_error($attachment_id)) {
        $fallback = cf_sideload_image_without_extension($url, $image_alt, $index);
        if (is_wp_error($fallback)) {
            $errors[] = $url . ' => ' . $attachment_id->get_error_code() . ':' . $attachment_id->get_error_message() . ' | fallback=' . $fallback->get_error_code() . ':' . $fallback->get_error_message();
            continue;
        }
        $uploaded[] = $fallback;
        continue;
    }
    if ($image_alt) {
        update_post_meta($attachment_id, '_wp_attachment_image_alt', $image_alt . ' - hình ' . ($index + 1));
    }
    $uploaded[] = array(
        'source_url' => $url,
        'id' => intval($attachment_id),
        'url' => wp_get_attachment_url($attachment_id),
        'alt' => get_post_meta($attachment_id, '_wp_attachment_image_alt', true),
    );
}

if (!$uploaded) {
    fwrite(STDERR, "No images could be uploaded\n");
    if ($errors) {
        fwrite(STDERR, implode("\n", $errors) . "\n");
    }
    exit(1);
}

echo wp_json_encode(
    array(
        'uploaded' => $uploaded,
    )
);
