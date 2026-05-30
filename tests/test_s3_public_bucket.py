"""Tests for public bucket (no-sign-request) and profile-based S3 access.

These tests verify that:
1. BgenReader can access public S3 buckets without credentials (no_sign_request)
2. BgenReader accepts AWS profile selection
3. BgenReader accepts UPatch-like objects for S3 configuration

The tests use a local Minio container configured with an anonymous-read policy
to simulate public bucket access.
"""

import os
import json
import unittest
from pathlib import Path

import numpy as np

from bgen import BgenReader

from tests.utils import load_gen_data, arrays_equal
from tests.minio_utils import (
    get_minio_client,
    is_minio_available,
    upload_test_data,
    MINIO_ENDPOINT,
    MINIO_ACCESS_KEY,
    MINIO_SECRET_KEY,
    MINIO_BUCKET,
    MINIO_SECURE,
)


PUBLIC_BUCKET = "publicdata"
SKIP_REASON = "Minio service not available"


def make_bucket_public(client, bucket_name):
    """Set a bucket policy that allows anonymous read access."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject", "s3:GetBucketLocation"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            }
        ],
    }
    client.set_bucket_policy(bucket_name, json.dumps(policy))


def setup_public_bucket():
    """Create and populate a public bucket in Minio."""
    client = get_minio_client()
    if not client.bucket_exists(PUBLIC_BUCKET):
        client.make_bucket(PUBLIC_BUCKET)
    make_bucket_public(client, PUBLIC_BUCKET)

    # Upload test data
    data_dir = Path(__file__).parent / "data"
    for filepath in data_dir.iterdir():
        if filepath.is_file():
            client.fput_object(PUBLIC_BUCKET, filepath.name, str(filepath))


def s3_url(filename, bucket=PUBLIC_BUCKET):
    """Construct an s3:// URL for a test file."""
    return f"s3://{bucket}/{filename}"


class UPatch:
    """A simple UPatch-like object that carries S3 configuration and credentials."""

    def __init__(self, region=None, endpoint=None, profile=None,
                 access_key=None, secret_key=None, session_token=None,
                 no_sign_request=False, use_ssl=True, path_style=False):
        self.region = region
        self.endpoint = endpoint
        self.profile = profile
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.no_sign_request = no_sign_request
        self.use_ssl = use_ssl
        self.path_style = path_style


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3PublicBucket(unittest.TestCase):
    """Tests for reading from public buckets without credentials (no-sign-request)."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        setup_public_bucket()

    def test_no_sign_request_opens_public_bucket(self):
        """Can open a public bucket bgen file with no_sign_request=True."""
        # Clear any AWS credentials from environment to prove they aren't needed
        env_backup = {}
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
            env_backup[key] = os.environ.pop(key, None)

        try:
            path = s3_url("example.16bits.zstd.bgen")
            with BgenReader(
                path,
                s3_endpoint=MINIO_ENDPOINT,
                s3_use_ssl=MINIO_SECURE,
                s3_path_style=True,
                s3_no_sign_request=True,
            ) as bfile:
                self.assertEqual(len(bfile.samples), 500)
                var = next(bfile)
                self.assertEqual(var.rsid, self.gen_data[0].rsid)
        finally:
            # Restore environment
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val

    def test_no_sign_request_reads_genotypes(self):
        """Genotype data is correct from public bucket with no-sign-request."""
        env_backup = {}
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
            env_backup[key] = os.environ.pop(key, None)

        try:
            path = s3_url("example.16bits.zstd.bgen")
            with BgenReader(
                path,
                s3_endpoint=MINIO_ENDPOINT,
                s3_use_ssl=MINIO_SECURE,
                s3_path_style=True,
                s3_no_sign_request=True,
            ) as bfile:
                for var, g in zip(bfile, self.gen_data):
                    self.assertEqual(g, var)
                    self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))
        finally:
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val

    def test_no_sign_request_iterate_variants(self):
        """Can iterate variants from public bucket without credentials."""
        env_backup = {}
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
            env_backup[key] = os.environ.pop(key, None)

        try:
            path = s3_url("example.16bits.zstd.bgen")
            with BgenReader(
                path,
                s3_endpoint=MINIO_ENDPOINT,
                s3_use_ssl=MINIO_SECURE,
                s3_path_style=True,
                s3_no_sign_request=True,
            ) as bfile:
                count = 0
                for var in bfile:
                    count += 1
                    if count >= 10:
                        break
                self.assertEqual(count, 10)
        finally:
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3UPatch(unittest.TestCase):
    """Tests for UPatch object support in BgenReader."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        setup_public_bucket()

    def test_upatch_with_credentials(self):
        """Can open S3 file using UPatch object with explicit credentials."""
        patch = UPatch(
            endpoint=MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            use_ssl=MINIO_SECURE,
            path_style=True,
            region="us-east-1",
        )
        path = s3_url("example.16bits.zstd.bgen", bucket=MINIO_BUCKET)
        with BgenReader(path, s3_patch=patch) as bfile:
            self.assertEqual(len(bfile.samples), 500)
            var = next(bfile)
            self.assertEqual(var.rsid, self.gen_data[0].rsid)

    def test_upatch_no_sign_request(self):
        """Can open public bucket using UPatch with no_sign_request."""
        env_backup = {}
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
            env_backup[key] = os.environ.pop(key, None)

        try:
            patch = UPatch(
                endpoint=MINIO_ENDPOINT,
                no_sign_request=True,
                use_ssl=MINIO_SECURE,
                path_style=True,
            )
            path = s3_url("example.16bits.zstd.bgen")
            with BgenReader(path, s3_patch=patch) as bfile:
                self.assertEqual(len(bfile.samples), 500)
        finally:
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val

    def test_upatch_overrides_env(self):
        """UPatch settings override environment variables."""
        # Set wrong credentials in env
        os.environ["AWS_ACCESS_KEY_ID"] = "wrong_key"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "wrong_secret"
        os.environ["BGEN_S3_ENDPOINT"] = "wrong_endpoint:9999"

        try:
            patch = UPatch(
                endpoint=MINIO_ENDPOINT,
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
                use_ssl=MINIO_SECURE,
                path_style=True,
                region="us-east-1",
            )
            path = s3_url("example.16bits.zstd.bgen", bucket=MINIO_BUCKET)
            with BgenReader(path, s3_patch=patch) as bfile:
                self.assertEqual(len(bfile.samples), 500)
        finally:
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            os.environ.pop("BGEN_S3_ENDPOINT", None)


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3ProfileSelection(unittest.TestCase):
    """Tests for AWS profile selection."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)

    def test_profile_parameter(self):
        """Can specify an AWS profile for credential lookup."""
        import tempfile

        # Create a temporary credentials file with a test profile
        creds_content = f"""[testprofile]
aws_access_key_id = {MINIO_ACCESS_KEY}
aws_secret_access_key = {MINIO_SECRET_KEY}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".credentials",
                                         delete=False) as f:
            f.write(creds_content)
            creds_file = f.name

        # Clear env credentials and point to our credentials file
        env_backup = {}
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_SESSION_TOKEN", "AWS_PROFILE",
                    "AWS_SHARED_CREDENTIALS_FILE"]:
            env_backup[key] = os.environ.pop(key, None)

        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = creds_file
        os.environ["BGEN_S3_ENDPOINT"] = MINIO_ENDPOINT
        os.environ["BGEN_S3_USE_SSL"] = "true" if MINIO_SECURE else "false"
        os.environ["BGEN_S3_PATH_STYLE"] = "true"

        try:
            path = s3_url("example.16bits.zstd.bgen", bucket=MINIO_BUCKET)
            with BgenReader(path, s3_profile="testprofile") as bfile:
                self.assertEqual(len(bfile.samples), 500)
        finally:
            os.unlink(creds_file)
            # Remove test env vars
            for key in ["AWS_SHARED_CREDENTIALS_FILE", "BGEN_S3_ENDPOINT",
                        "BGEN_S3_USE_SSL", "BGEN_S3_PATH_STYLE"]:
                os.environ.pop(key, None)
            # Restore original env
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val
