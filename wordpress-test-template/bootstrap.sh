#!/bin/zsh
set -euo pipefail

SITE_URL="${SITE_URL:-http://localhost:8090}"
SITE_TITLE="${SITE_TITLE:-WordPress Test}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123456}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

docker-compose up -d db wordpress

until docker-compose exec -T db mariadb-admin ping -h127.0.0.1 -uroot -prootpass --silent; do
  sleep 3
done

mkdir -p site

docker run --rm \
  --network wordpress-test_default \
  -v "$PWD/site:/var/www/html" \
  wordpress:cli \
  wp core download --allow-root

if [ ! -f site/wp-config.php ]; then
  docker run --rm \
    --network wordpress-test_default \
    -v "$PWD/site:/var/www/html" \
    wordpress:cli \
    wp config create \
    --dbname=wordpress \
    --dbuser=wordpress \
    --dbpass=wordpress \
    --dbhost=db:3306 \
    --allow-root
fi

if ! docker run --rm \
  --network wordpress-test_default \
  -v "$PWD/site:/var/www/html" \
  wordpress:cli \
  wp core is-installed --allow-root >/dev/null 2>&1; then
  docker run --rm \
    --network wordpress-test_default \
    -v "$PWD/site:/var/www/html" \
    wordpress:cli \
    wp core install \
    --url="$SITE_URL" \
    --title="$SITE_TITLE" \
    --admin_user="$ADMIN_USER" \
    --admin_password="$ADMIN_PASSWORD" \
    --admin_email="$ADMIN_EMAIL" \
    --skip-email \
    --allow-root
fi

docker run --rm \
  --network wordpress-test_default \
  -v "$PWD/site:/var/www/html" \
  wordpress:cli \
  wp plugin install woocommerce seo-by-rank-math --activate --allow-root

docker run --rm \
  --network wordpress-test_default \
  -v "$PWD/site:/var/www/html" \
  wordpress:cli \
  wp option update blogdescription "WordPress test with WooCommerce and Rank Math" --allow-root

echo "Site ready at $SITE_URL"
echo "Admin user: $ADMIN_USER"
echo "Admin password: $ADMIN_PASSWORD"
