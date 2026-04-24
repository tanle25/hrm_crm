<?php

$encoded = getenv('PRODUCT_PAYLOAD_B64');
if (!$encoded) {
    fwrite(STDERR, "Missing PRODUCT_PAYLOAD_B64\n");
    exit(1);
}

$payload = json_decode(base64_decode($encoded), true);
if (!is_array($payload)) {
    fwrite(STDERR, "Invalid payload\n");
    exit(1);
}

if (!class_exists('WC_Product_Simple') || !class_exists('WC_Product_Variable')) {
    fwrite(STDERR, "WooCommerce is not loaded\n");
    exit(1);
}

$product_type = $payload['type'] ?? 'simple';
$product = $product_type === 'variable' ? new WC_Product_Variable() : new WC_Product_Simple();
$product->set_name($payload['name'] ?? 'Untitled Product');
$product->set_slug($payload['slug'] ?? sanitize_title($payload['name'] ?? 'product'));
$product->set_status($payload['status'] ?? 'draft');
$product->set_catalog_visibility('visible');
$product->set_description($payload['description'] ?? '');
$product->set_short_description($payload['short_description'] ?? '');
if ($product_type !== 'variable') {
    $product->set_regular_price((string) ($payload['regular_price'] ?? '0'));
    $product->set_price((string) ($payload['regular_price'] ?? '0'));
}

if (!empty($payload['category_ids']) && is_array($payload['category_ids'])) {
    $product->set_category_ids(array_map('intval', $payload['category_ids']));
}

if (!empty($payload['tags']) && is_array($payload['tags'])) {
    $tag_ids = array();
    foreach ($payload['tags'] as $tag_payload) {
        $tag_name = is_array($tag_payload) ? sanitize_text_field($tag_payload['name'] ?? '') : sanitize_text_field((string) $tag_payload);
        if (!$tag_name) {
            continue;
        }
        $term = term_exists($tag_name, 'product_tag');
        if (!$term) {
            $term = wp_insert_term($tag_name, 'product_tag');
        }
        if (is_wp_error($term)) {
            continue;
        }
        $tag_ids[] = intval(is_array($term) ? $term['term_id'] : $term);
        if (count($tag_ids) >= 5) {
            break;
        }
    }
    if (!empty($tag_ids)) {
        $product->set_tag_ids(array_values(array_unique($tag_ids)));
    }
}

if ($product_type === 'variable' && !empty($payload['attributes']) && is_array($payload['attributes'])) {
    $wc_attributes = array();
    foreach ($payload['attributes'] as $attribute_payload) {
        $name = sanitize_text_field($attribute_payload['name'] ?? '');
        $options = $attribute_payload['options'] ?? array();
        if (!$name || !is_array($options) || empty($options)) {
            continue;
        }
        $attribute = new WC_Product_Attribute();
        $attribute->set_id(0);
        $attribute->set_name($name);
        $attribute->set_options(array_values(array_map('sanitize_text_field', $options)));
        $attribute->set_visible(!empty($attribute_payload['visible']));
        $attribute->set_variation(!empty($attribute_payload['variation']));
        $wc_attributes[] = $attribute;
    }
    if (!empty($wc_attributes)) {
        $product->set_attributes($wc_attributes);
    }
}

$product_id = $product->save();

if ($product_type === 'variable' && !empty($payload['variations']) && is_array($payload['variations'])) {
    foreach ($payload['variations'] as $variation_payload) {
        if (empty($variation_payload['attributes']) || !is_array($variation_payload['attributes'])) {
            continue;
        }
        $variation = new WC_Product_Variation();
        $variation->set_parent_id($product_id);
        $variation_attributes = array();
        foreach ($variation_payload['attributes'] as $attribute_payload) {
            $name = sanitize_title($attribute_payload['name'] ?? '');
            $option = sanitize_text_field($attribute_payload['option'] ?? '');
            if ($name && $option) {
                $variation_attributes[$name] = $option;
            }
        }
        if (empty($variation_attributes)) {
            continue;
        }
        $variation->set_attributes($variation_attributes);
        if (isset($variation_payload['regular_price']) && $variation_payload['regular_price'] !== '') {
            $variation->set_regular_price((string) $variation_payload['regular_price']);
            $variation->set_price((string) $variation_payload['regular_price']);
        }
        $variation->set_status('publish');
        $variation->save();
    }
    WC_Product_Variable::sync($product_id);
}

if ( ! empty( $payload['featured_image_id'] ) ) {
    $attachment_id = intval( $payload['featured_image_id'] );
    set_post_thumbnail( $product_id, $attachment_id );
    if ( ! empty( $payload['image_alt'] ) ) {
        update_post_meta( $attachment_id, '_wp_attachment_image_alt', $payload['image_alt'] );
    }
} elseif ( ! empty( $payload['image_url'] ) ) {
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $attachment_id = media_sideload_image( $payload['image_url'], $product_id, null, 'id' );
    if ( ! is_wp_error( $attachment_id ) ) {
        set_post_thumbnail( $product_id, $attachment_id );
        if ( ! empty( $payload['image_alt'] ) ) {
            update_post_meta( $attachment_id, '_wp_attachment_image_alt', $payload['image_alt'] );
        }
    }
}

if ( ! empty( $payload['gallery_image_ids'] ) && is_array( $payload['gallery_image_ids'] ) ) {
    $gallery_ids = array_values( array_unique( array_filter( array_map( 'intval', $payload['gallery_image_ids'] ) ) ) );
    if ( ! empty( $gallery_ids ) ) {
        if ( ! empty( $payload['image_alt'] ) ) {
            foreach ( $gallery_ids as $index => $gallery_id ) {
                update_post_meta( $gallery_id, '_wp_attachment_image_alt', $payload['image_alt'] . ' - hình ' . ( $index + 1 ) );
            }
        }
        $featured_id = ! empty( $payload['featured_image_id'] ) ? intval( $payload['featured_image_id'] ) : 0;
        $gallery_only = array_values( array_filter( $gallery_ids, fn( $id ) => $id !== $featured_id ) );
        update_post_meta( $product_id, '_product_image_gallery', implode( ',', $gallery_only ) );
    }
}

if (!empty($payload['meta']) && is_array($payload['meta'])) {
    foreach ($payload['meta'] as $meta_key => $meta_value) {
        update_post_meta($product_id, $meta_key, $meta_value);
    }
}

echo wp_json_encode(
    array(
        'id'   => $product_id,
        'link' => get_permalink($product_id),
    )
);
