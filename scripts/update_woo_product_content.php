<?php

$product_id = intval(getenv('PRODUCT_ID') ?: 0);
$content_b64 = getenv('CONTENT_B64') ?: '';
$meta_b64 = getenv('META_B64') ?: '';

if (!$product_id || !$content_b64) {
    fwrite(STDERR, "Missing PRODUCT_ID or CONTENT_B64\n");
    exit(1);
}

$content = base64_decode($content_b64);
$meta = array();
if ($meta_b64) {
    $decoded = json_decode(base64_decode($meta_b64), true);
    if (is_array($decoded)) {
        $meta = $decoded;
    }
}

$updated = wp_update_post(
    array(
        'ID' => $product_id,
        'post_content' => $content,
    ),
    true
);

if (is_wp_error($updated)) {
    fwrite(STDERR, $updated->get_error_message() . "\n");
    exit(1);
}

foreach ($meta as $key => $value) {
    update_post_meta($product_id, $key, is_array($value) ? wp_json_encode($value, JSON_UNESCAPED_UNICODE) : $value);
}

echo wp_json_encode(array('product_id' => $product_id, 'updated' => true));
