# Wordpress Shop Setup Service

Provision WooCommerce multisite shops with correct rewrites and checkout working, using FastAPI and Docker.  
Supports Localized Interface and translation workflow.

## Features

- FastAPI REST API for shop provisioning
- Docker-based WordPress and MySQL setup
- Automatic WooCommerce installation and demo product import
- Multisite network with custom rewrite rules for subsites
- Export WooCommerce translation strings to CSV
- Upload translated CSV to generate PO/MO and deploy to WooCommerce language directory

## Requirements

- Python 3.11 (recommended: use conda)
- Docker
- [requirements.txt](requirements.txt) dependencies

## Setup

```sh
conda create -n env-wordpress-shop python=3.11 -y
conda activate env-wordpress-shop
pip install -r requirements.txt
```

## Running

Start the API server with:
```sh
uvicorn app:app --reload --host 0.0.0.0 --port 5000
```

## Usage

### Create Shop

Send a POST request to `/create_shop` with JSON body:
```json
{
  "site_name": "My Shop",
  "email": "admin@example.com",
  "tenant_mode": "multi",
  "theme": "woostify",
  "locale": "en_US",
  "wp_image": "wordpress:6.7-php8.2-apache",
  "mysql_image": "mysql:5.7"
}
```

Example using curl:
```sh
curl -X POST http://localhost:5000/create_shop \
  -H "Content-Type: application/json" \
  -d '{"site_name":"My Shop"}'
```

### Export WooCommerce Strings

Download translation strings as CSV:
```sh
curl -O http://localhost:5000/download_csv/{store_name}
```

### Upload Translated CSV

Upload a translated CSV to generate PO/MO and deploy:
```sh
curl -X POST http://localhost:5000/upload_csv/{store_name} \
  -F "file=@woocommerce.csv"
```

## Notes

- The WordPress container exposes port 8080.
- Latest stable image version: `wordpress:6.7-php8.2-apache`
- Logs are written to files named like `log_YYYYMMDD_HHMMSS.log`.
- Translation workflow uses [polib](https://github.com/translate/polib) for PO/MO file handling.

## License

MIT

