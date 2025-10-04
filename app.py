import os
import re
import io
import csv
import json
import tarfile
import time
import logging
import subprocess
from datetime import datetime
from tempfile import NamedTemporaryFile
from typing import Optional, List

import sys
import docker
import polib
import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel


# ============================
# Logging
# ============================
log_filename = f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_filename, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logger.info(f"📄 Log file created: {log_filename}")

# ============================
# Docker Client
# ============================
client = docker.from_env()

# ============================
# Constants
# ============================
SHARED_NETWORK = "shared_net_shop"
SHARED_DB_NAME = "shared_db_shop"
SHARED_WP_NAME = "shared_wp_shop"

WOO_DEMO_URL = (
    "https://raw.githubusercontent.com/woocommerce/woocommerce/trunk/"
    "plugins/woocommerce/sample-data/sample_products.xml"
)
WOO_DEMO_PATH = "/var/www/html/demo.xml"
WOO_ZIP_URL = "https://downloads.wordpress.org/plugin/woocommerce.8.6.1.zip"

PAYMENT_LABELS = {
    "en_US": {
        "bacs_title": "Bank Transfer",
        "bacs_instructions": "Please transfer the amount to our bank account.",
        "cod_title": "Cash on Delivery",
        "cod_instructions": "Pay with cash upon delivery.",
        "paypal_title": "PayPal",
    },
    "zh_TW": {
        "bacs_title": "銀行轉帳",
        "bacs_instructions": "請將款項轉入以下帳號。",
        "cod_title": "貨到付款",
        "cod_instructions": "收到商品後請支付款項。",
        "paypal_title": "PayPal",
    },
    "zh_CN": {
        "bacs_title": "银行转账",
        "bacs_instructions": "请将款项转入以下账户。",
        "cod_title": "货到付款",
        "cod_instructions": "收到商品后请支付款项。",
        "paypal_title": "贝宝支付",
    },
    "zh_HK": {
        "bacs_title": "銀行轉帳",
        "bacs_instructions": "請將款項轉入以下賬戶。",
        "cod_title": "貨到付款",
        "cod_instructions": "收到貨品後請支付款項。",
        "paypal_title": "PayPal",
    },
}


# ============================
# Models
# ============================
class CompanyInfo(BaseModel):
    name: str
    address_1: str
    address_2: Optional[str] = ""
    city: Optional[str] = ""
    country: str
    postcode: Optional[str] = ""
    email: Optional[str] = ""


class ShopRequest(BaseModel):
    site_name: str
    email: str = "admin@example.com"
    tenant_mode: str = "multi"
    theme: str = "woostify"
    locale: str = "en_US"
    company: Optional[List[CompanyInfo]] = None
    wp_image: str = "wordpress:6.7-php8.2-apache"
    mysql_image: str = "mysql:5.7"
    extra_gateways: Optional[List[str]] = []
    payment_settings: Optional[dict] = {}


# ============================
# Helper
# ============================
def sanitize_store_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "site"


def wait_for_mysql(container: str, user: str, pw: str, db: str, timeout=120) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            cmd = [
                "docker",
                "exec",
                container,
                "mysql",
                f"-u{user}",
                f"-p{pw}",
                "-e",
                f"USE {db};",
            ]
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def run_wp_cli(container, cmd: str) -> str:
    logger.info(f"▶ Running: {cmd}")
    result = container.exec_run(cmd, user="root", workdir="/var/www/html")
    output = result.output.decode("utf-8")
    if result.exit_code != 0:
        logger.error(f"❌ Error: {output}")
    else:
        logger.info(f"✔ Output: {output.strip()}")
    return output


def ensure_wp_cli(container):
    if container.exec_run("wp --info --allow-root", workdir="/var/www/html").exit_code != 0:
        logger.info("🔧 Installing wp-cli...")
        container.exec_run(
            "bash -c 'apt-get update && apt-get install -y less mariadb-client curl ca-certificates && "
            "curl -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar && "
            "chmod +x /usr/local/bin/wp'",
            user="root",
        )
    container.exec_run("bash -c 'apt-get update && apt-get install -y unzip php-xml'", user="root")
    container.exec_run(
        "bash -c \"echo 'memory_limit = 512M' > /usr/local/etc/php/conf.d/memory-limit.ini\"",
        user="root",
    )


def ensure_network_core(wp, site_url, title, email):
    if "WordPress is installed" not in run_wp_cli(wp, "wp core is-installed --allow-root"):
        return run_wp_cli(
            wp,
            f"wp core multisite-install --url={site_url} --title='{title}' "
            f"--admin_user=admin --admin_password=admin123 --admin_email={email} "
            f"--skip-email --allow-root",
        )
    return "Network exists"


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


def copy_to_container(container, local_file: str, remote_dir: str):
    container.exec_run(f"mkdir -p {remote_dir}", user="root")
    tarstream = io.BytesIO()
    basename = os.path.basename(local_file)
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        with open(local_file, "rb") as f:
            data = f.read()
            info = tarfile.TarInfo(name=basename)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tarstream.seek(0)
    container.put_archive(remote_dir, tarstream.getvalue())
    tarstream.close()

# ============================
# Provision
# ============================
def provision_multi(req: ShopRequest):
    store_name = sanitize_store_name(req.site_name)
    try:
        client.networks.get(SHARED_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(SHARED_NETWORK, driver="bridge")

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
        f'wp site create --slug={store_name} --title="{req.site_name}" '
        f'--email={req.email} --allow-root',
    )

    return {"slug": store_name, "url": f"http://localhost:8080/{store_name}"}


# ============================
# Setup Shop
# ============================


def setup_shop(wp, site_url: str, theme: str, locale: str,
               company: Optional[List[CompanyInfo]],
               extra_gateways: Optional[List[str]] = None,
               payment_settings: Optional[dict] = None):

    logger.info("===== 🚀 Setup Shop Started =====")
    logger.info(f"Site URL: {site_url}, Theme: {theme}, Locale: {locale}")
    run_wp_cli(wp, f"wp theme install {theme} --activate --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp plugin install {WOO_ZIP_URL} --force --activate --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp plugin install wordpress-importer --activate --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp wc tool run install_pages --user=admin --url={site_url} --allow-root")

    wp.exec_run(f"curl -o {WOO_DEMO_PATH} {WOO_DEMO_URL}", user="root")
    run_wp_cli(wp, f"wp import {WOO_DEMO_PATH} --authors=create --url={site_url} --allow-root")

    # Set locale (Chinese packages)

    logger.info(f"===== ✅ Setup locale {locale} Start =====")
    run_wp_cli(wp, f"wp language core install {locale} --allow-root")
    run_wp_cli(wp, f"wp site switch-language {locale} --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp option update WPLANG {locale} --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp language plugin install woocommerce {locale} --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp language theme install {theme} {locale} --url={site_url} --allow-root")
    run_wp_cli(wp, "wp language core update --allow-root")
    logger.info(f"===== ✅ Setup locale {locale} End =====")

    # Payment Gateways
    labels = PAYMENT_LABELS.get(locale, PAYMENT_LABELS["en_US"])
    paypal_email = company[0].email if (company and company[0].email) else "paypal@example.com"

    run_wp_cli(wp, f"wp option patch update woocommerce_paypal_settings enabled yes --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp option patch update woocommerce_paypal_settings title \"{labels['paypal_title']}\" --url={site_url} --allow-root")
    run_wp_cli(wp, f"wp option patch update woocommerce_paypal_settings email \"{paypal_email}\" --url={site_url} --allow-root")

    run_wp_cli(
        wp,
        f"""wp eval "update_option('woocommerce_cod_settings', array(
            'enabled' => 'yes',
            'title' => '{labels['cod_title']}',
            'instructions' => '{labels['cod_instructions']}'
        ));" --url={site_url} --allow-root""",
    )
    run_wp_cli(
        wp,
        f"""wp eval "update_option('woocommerce_bacs_settings', array(
            'enabled' => 'yes',
            'title' => '{labels['bacs_title']}',
            'instructions' => '{labels['bacs_instructions']}'
        ));" --url={site_url} --allow-root""",
    )

    # Extra gateways
    if extra_gateways:
        for gw in extra_gateways:
            try:
                run_wp_cli(wp, f"wp plugin install woocommerce-gateway-{gw} --activate --allow-root")
            except Exception as e:
                logger.warning(f"🚫 Failed install {gw}: {e}")

        if "stripe" in extra_gateways and payment_settings.get("stripe"):
            stripe = payment_settings["stripe"]
            run_wp_cli(
                wp,
                f"""wp eval "update_option('woocommerce_stripe_settings', array(
                    'enabled' => 'yes',
                    'title' => 'Stripe',
                    'publishable_key' => '{stripe.get('publishable_key')}',
                    'secret_key' => '{stripe.get('secret_key')}'
                ));" --url={site_url} --allow-root""",
            )

        if "wechat" in extra_gateways and payment_settings.get("wechat"):
            wechat = payment_settings["wechat"]
            run_wp_cli(
                wp,
                f"""wp eval "update_option('woocommerce_wechat_settings', array(
                    'enabled' => 'yes',
                    'title' => 'WeChat Pay',
                    'mch_id' => '{wechat.get('mch_id')}',
                    'api_key' => '{wechat.get('api_key')}'
                ));" --url={site_url} --allow-root""",
            )

    # Shipping
    run_wp_cli(wp, f"wp wc shipping_zone create --name='Default Zone' --user=admin --url={site_url} --allow-root")
    run_wp_cli(wp, f"""wp eval "WC_Shipping_Zones::get_zone(1)->add_shipping_method('flat_rate');" --url={site_url} --allow-root""")

    logger.info("===== ✅ Setup Shop Completed =====")
    return {"theme": theme, "locale": locale, "gateways": extra_gateways or []}


# ============================
# FastAPI
# ============================
app = FastAPI(title="WordPress Shop Setup Service", version="37.0.0")


@app.post("/create_shop")
def create_shop_endpoint(req: ShopRequest):
    if req.tenant_mode != "multi":
        raise HTTPException(status_code=400, detail="Only multi-tenant mode supported")
    logger.info(f"⚙️ API /create_shop called for {req.site_name}")
    site = provision_multi(req)
    wp = client.containers.get(SHARED_WP_NAME)
    setup = setup_shop(
        wp,
        site["url"],
        req.theme,
        req.locale,
        req.company,
        req.extra_gateways,
        req.payment_settings,
    )
    return {"site": site, "setup": setup}