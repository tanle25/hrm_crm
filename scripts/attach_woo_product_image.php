<?php

$product_id = intval(getenv('PRODUCT_ID') ?: 0);
$image_url  = getenv('IMAGE_URL') ?: '';
$image_urls_b64 = getenv('IMAGE_URLS_B64') ?: '';
$image_alt  = getenv('IMAGE_ALT') ?: '';

if (!$product_id || (!$image_url && !$image_urls_b64)) {
    fwrite(STDERR, "Missing PRODUCT_ID or image URL data\n");
    exit(1);
}

require_once ABSPATH . 'wp-admin/includes/media.php';
require_once ABSPATH . 'wp-admin/includes/file.php';
require_once ABSPATH . 'wp-admin/includes/image.php';

$image_urls = array();
if ($image_urls_b64) {
    $decoded = json_decode(base64_decode($image_urls_b64), true);
    if (is_array($decoded)) {
        $image_urls = $decoded;
    }
}
if (!$image_urls && $image_url) {
    $image_urls = array($image_url);
}

$attachment_ids = array();
$uploaded = array();
foreach (array_slice(array_unique(array_filter($image_urls)), 0, 8) as $url) {
    $attachment_id = media_sideload_image($url, $product_id, null, 'id');
    if (is_wp_error($attachment_id)) {
        continue;
    }
    $attachment_ids[] = intval($attachment_id);
    $uploaded[] = array(
        'source_url' => $url,
        'id' => intval($attachment_id),
        'url' => wp_get_attachment_url($attachment_id),
    );
    if ($image_alt) {
        update_post_meta($attachment_id, '_wp_attachment_image_alt', $image_alt);
    }
}

if (!$attachment_ids) {
    fwrite(STDERR, "No images could be sideloaded\n");
    exit(1);
}

set_post_thumbnail($product_id, $attachment_ids[0]);
if (count($attachment_ids) > 1) {
    update_post_meta($product_id, '_product_image_gallery', implode(',', array_slice($attachment_ids, 1)));
}

echo wp_json_encode(
	    array(
	        'product_id'    => $product_id,
	        'attachment_id' => $attachment_ids[0],
	        'gallery_ids'   => array_slice($attachment_ids, 1),
	        'uploaded'      => $uploaded,
	    )
	);
