from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import docker, time, re, subprocess

app = FastAPI(
    title="WordPress Shop Setup Service",
    description="Provision WooCommerce with multi-tenant support, themes, demo data and support localization by providing locale.",
    version="16.0.0"
)

client = docker.from_env()

# -----------------------------
# Shared constants
# -----------------------------
SHARED_NETWORK = "shared_net_shop"
SHARED_DB_NAME = "shared_db_shop"
SHARED_WP_NAME = "shared_wp_shop"

WOO_DEMO_URL = "https://raw.githubusercontent.com/woocommerce/woocommerce/trunk/plugins/woocommerce/sample-data/sample_products.xml"
WOO_DEMO_PATH = "/var/www/html/demo.xml"
WOO_ZIP_URL = "https://downloads.wordpress.org/plugin/woocommerce.8.6.1.zip"

# -----------------------------
# Data Models
# -----------------------------
class ShopRequest(BaseModel):
    site_name: str
    email: str = "admin@example.com"
    tenant_mode: str = "multi"
    theme: str = "woostify"
    locale: str = "en_US"  # zh_TW / zh_CN / zh_HK / en_US
    wp_image: str = "wordpress:6.7-php8.2-apache"
    mysql_image: str = "mysql:5.7"


# -----------------------------
# Helpers
# -----------------------------
def sanitize_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "site"

def wait_for_mysql(container: str, user: str, pw: str, db: str, timeout: int = 120) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            cmd = ["docker", "exec", container, "mysql", f"-u{user}", f"-p{pw}", "-e", f"USE {db};"]
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

def run_wp_cli(container, cmd: str) -> str:
    out = container.exec_run(cmd, user="root", workdir="/var/www/html")
    return out.output.decode("utf-8")

def ensure_wp_cli(container):
    check = container.exec_run("wp --info --allow-root", workdir="/var/www/html")
    if check.exit_code != 0:
        container.exec_run(
            "bash -c 'apt-get update && apt-get install -y less mariadb-client curl ca-certificates "
            "&& curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar "
            "&& chmod +x /usr/local/bin/wp'",
            user="root",
        )
    container.exec_run("bash -c 'apt-get update && apt-get install -y unzip php-xml'", user="root")

def ensure_network_core(wp, site_url, title, email):
    check = run_wp_cli(wp, "wp core is-installed --allow-root")
    if "WordPress is installed" not in check:
        return run_wp_cli(
            wp,
            f"wp core multisite-install --url={site_url} --title='{title}' "
            f"--admin_user=admin --admin_password=admin123 "
            f"--admin_email={email} --skip-email --allow-root",
        )
    return "Network already installed"

def enable_rewrite_for_subsites(wp):
    htaccess = r"""
# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteBase /
RewriteRule ^index\.php$ - [L]
RewriteRule ^([_0-9a-zA-Z-]+/)?wp-content/(.*)$ wp-content/$2 [L]
RewriteRule ^([_0-9a-zA-Z-]+/)?wp-includes/(.*)$ wp-includes/$2 [L]
RewriteRule ^([_0-9a-zA-Z-]+/)?wp-admin/admin-ajax\.php$ wp-admin/admin-ajax.php [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress
"""
    wp.exec_run(f"bash -c \"echo '{htaccess}' > /var/www/html/.htaccess\"", user="root")
    return "Rewrites enabled"


# -----------------------------
# Shop Setup
# -----------------------------
def setup_shop(wp, site_url: str, theme: str, locale: str):
    results = {}

    # Theme
    run_wp_cli(wp, f"wp theme install {theme} --activate --url={site_url} --allow-root")
    results["theme"] = f"{theme} active"

    # Plugins
    run_wp_cli(wp, f"wp plugin install {WOO_ZIP_URL} --force --activate --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp plugin install wordpress-importer --activate --url={site_url} --allow-root")

    # WooCommerce Pages
    pages = {
        "shop": ("Shop", ""),
        "cart": ("Cart", "[woocommerce_cart]"),
        "checkout": ("Checkout", "[woocommerce_checkout]"),
        "my-account": ("My Account", "[woocommerce_my_account]"),
    }
    page_ids = {}
    for slugp, (title, content) in pages.items():
        pid = run_wp_cli(
            wp, f"wp post list --post_type=page --name={slugp} "
                f"--field=ID --url={site_url} --allow-root"
        ).strip()
        if not pid:
            pid = run_wp_cli(
                wp,
                f"wp post create --post_type=page --post_title='{title}' "
                f"--post_name={slugp} --post_status=publish "
                f"--post_content='{content}' "
                f"--url={site_url} --allow-root --porcelain"
            ).strip()
        page_ids[slugp] = pid

    opts = {
        "shop": "woocommerce_shop_page_id",
        "cart": "woocommerce_cart_page_id",
        "checkout": "woocommerce_checkout_page_id",
        "my-account": "woocommerce_myaccount_page_id",
    }
    for slugp, opt in opts.items():
        run_wp_cli(wp, f"wp option update {opt} {page_ids[slugp]} --url={site_url} --allow-root")

    # Demo products
    run_wp_cli(wp, f"curl -L -o {WOO_DEMO_PATH} {WOO_DEMO_URL}")
    res = run_wp_cli(
        wp, f"wp import {WOO_DEMO_PATH} --authors=create --user=admin "
            f"--url={site_url} --allow-root"
    )
    results["demo"] = "products imported" if "error" not in res.lower() else "dummy created"

    # Homepage
    run_wp_cli(wp, f"wp option update show_on_front page --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp option update page_on_front {page_ids['shop']} --url={site_url} --allow-root")

    # 🔑 Install & switch site language
    run_wp_cli(wp, f"wp language core install {locale} --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp option update WPLANG {locale} --url={site_url} --allow-root")
    results["language"] = f"Site language set to {locale}"

    # 🔑 Install WooCommerce language pack
    run_wp_cli(wp, f"wp language plugin install woocommerce {locale} --url={site_url} --allow-root")
    results["woocommerce_lang"] = f"WooCommerce language pack installed for {locale}"

    # 🔑 Auto WooCommerce currency mapping
    currency_map = {
        "zh_TW": "TWD",  # 台灣
        "zh_CN": "CNY",  # 中國
        "zh_HK": "HKD",  # 香港
        "en_US": "USD"   # 英文
    }
    if locale in currency_map:
        run_wp_cli(
            wp,
            f"wp option update woocommerce_currency {currency_map[locale]} "
            f"--url={site_url} --allow-root"
        )
        results["currency"] = f"WooCommerce currency set to {currency_map[locale]}"

    return results


# -----------------------------
# Provision
# -----------------------------
def provision_multi(req: ShopRequest):
    slug = sanitize_slug(req.site_name)

    # network
    try:
        client.networks.get(SHARED_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(SHARED_NETWORK, driver="bridge")

    # DB
    try:
        client.containers.get(SHARED_DB_NAME)
    except docker.errors.NotFound:
        client.containers.run(
            req.mysql_image,
            name=SHARED_DB_NAME,
            environment={
                "MYSQL_ROOT_PASSWORD": "rootpw",
                "MYSQL_DATABASE": "wordpress",
                "MYSQL_USER": "wpuser",
                "MYSQL_PASSWORD": "wppass",
            },
            network=SHARED_NETWORK,
            detach=True,
            restart_policy={"Name": "always"},
        )
    if not wait_for_mysql(SHARED_DB_NAME, "wpuser", "wppass", "wordpress"):
        raise HTTPException(status_code=500, detail="MySQL not ready")

    # WP container
    try:
        wp = client.containers.get(SHARED_WP_NAME)
    except docker.errors.NotFound:
        wp = client.containers.run(
            req.wp_image,
            name=SHARED_WP_NAME,
            environment={
                "WORDPRESS_DB_HOST": f"{SHARED_DB_NAME}:3306",
                "WORDPRESS_DB_NAME": "wordpress",
                "WORDPRESS_DB_USER": "wpuser",
                "WORDPRESS_DB_PASSWORD": "wppass",
            },
            network=SHARED_NETWORK,
            ports={"80/tcp": 8080},
            detach=True,
            restart_policy={"Name": "always"},
        )

    ensure_wp_cli(wp)
    ensure_network_core(wp, "http://localhost:8080", req.site_name, req.email)
    enable_rewrite_for_subsites(wp)

    run_wp_cli(
        wp,
        f'wp site create --slug={slug} --title="{req.site_name}" '
        f'--email={req.email} --allow-root'
    )

    return {"slug": slug, "url": f"http://localhost:8080/{slug}"}


# -----------------------------
# Endpoint
# -----------------------------
@app.post("/create_shop")
def create_shop(req: ShopRequest):
    if req.tenant_mode != "multi":
        raise HTTPException(status_code=400, detail="Only multi tenant supported")

    site = provision_multi(req)
    wp = client.containers.get(SHARED_WP_NAME)
    setup = setup_shop(wp, site["url"], req.theme, req.locale)
    return {"site": site, "setup": setup}