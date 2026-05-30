"""Utilities for Minio-based testing.

This module provides helpers to upload test data to a Minio container and
download files from it, enabling tests that mimic the existing local-file
tests but with data hosted in an S3-compatible object store.
"""

import os
import tempfile
from pathlib import Path

from minio import Minio
from minio.error import S3Error

# Default Minio connection settings (matching docker-compose / CI service)
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "testdata")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"


def get_minio_client():
    """Create and return a Minio client instance."""
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def is_minio_available():
    """Check if the Minio service is reachable."""
    try:
        client = get_minio_client()
        client.list_buckets()
        return True
    except Exception:
        return False


def ensure_bucket_exists(client, bucket_name=MINIO_BUCKET):
    """Create the test bucket if it does not exist."""
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)


def upload_test_data(client, bucket_name=MINIO_BUCKET):
    """Upload all files from the tests/data directory to the Minio bucket."""
    data_dir = Path(__file__).parent / "data"
    ensure_bucket_exists(client, bucket_name)

    for filepath in data_dir.iterdir():
        if filepath.is_file():
            object_name = filepath.name
            client.fput_object(bucket_name, object_name, str(filepath))


def download_file(client, object_name, dest_dir, bucket_name=MINIO_BUCKET):
    """Download a single file from Minio to a local directory.

    Args:
        client: Minio client instance
        object_name: Name of the object in the bucket
        dest_dir: Local directory to download to
        bucket_name: Name of the Minio bucket

    Returns:
        Path to the downloaded file
    """
    dest_path = Path(dest_dir) / object_name
    client.fget_object(bucket_name, object_name, str(dest_path))
    return dest_path


def download_all_test_data(client, dest_dir, bucket_name=MINIO_BUCKET):
    """Download all objects from the test bucket to a local directory.

    Args:
        client: Minio client instance
        dest_dir: Local directory to download to
        bucket_name: Name of the Minio bucket

    Returns:
        Path object for the destination directory
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    objects = client.list_objects(bucket_name)
    for obj in objects:
        download_file(client, obj.object_name, dest_dir, bucket_name)

    return dest
