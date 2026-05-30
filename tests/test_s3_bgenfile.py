"""Tests for native S3 support in BgenReader.

These tests verify that BgenReader can open bgen files directly from S3
via s3:// URLs, using the C++ libcurl-based S3 stream implementation.
The tests use a local Minio container as the S3-compatible backend.

The tests are skipped if no Minio service is available.
"""

import os
import unittest
from pathlib import Path

import numpy as np

from bgen import BgenReader

from tests.utils import (
    load_gen_data,
    load_vcf_data,
    load_haps_data,
    arrays_equal,
)
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

SKIP_REASON = "Minio service not available"


def s3_url(filename):
    """Construct an s3:// URL for a test file."""
    return f"s3://{MINIO_BUCKET}/{filename}"


def setup_s3_env():
    """Set environment variables for S3 access to the Minio container."""
    os.environ["AWS_ACCESS_KEY_ID"] = MINIO_ACCESS_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = MINIO_SECRET_KEY
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["BGEN_S3_ENDPOINT"] = MINIO_ENDPOINT
    os.environ["BGEN_S3_USE_SSL"] = "true" if MINIO_SECURE else "false"
    os.environ["BGEN_S3_PATH_STYLE"] = "true"  # Minio requires path-style


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3BgenReader(unittest.TestCase):
    """Tests for BgenReader reading directly from S3 URLs."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        setup_s3_env()

    def test_open_s3_url(self):
        """Can open a bgen file via s3:// URL."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile.samples) > 0)
            self.assertEqual(len(bfile.samples), 500)

    def test_iterate_variants(self):
        """Can iterate through variants from an S3-hosted bgen."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            count = 0
            for var in bfile:
                count += 1
                if count >= 5:
                    break
            self.assertEqual(count, 5)

    def test_variant_metadata(self):
        """Variant metadata is correct from S3-hosted bgen."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            var = next(bfile)
            g = self.gen_data[0]
            self.assertEqual(var.rsid, g.rsid)
            self.assertEqual(var.chrom, g.chrom)
            self.assertEqual(var.pos, g.pos)

    def test_genotype_probabilities(self):
        """Genotype probabilities are correct from S3-hosted bgen."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            for var, g in zip(bfile, self.gen_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))

    def test_minor_allele_dosage(self):
        """minor_allele_dosage works for S3-hosted files."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            var = next(bfile)
            dose = var.minor_allele_dosage
            self.assertTrue(isinstance(dose, np.ndarray))
            self.assertEqual(len(dose), 500)

    def test_alt_dosage(self):
        """alt_dosage works for S3-hosted files."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path, delay_parsing=True) as bfile:
            var = next(bfile)
            dose = var.alt_dosage
            probs = var.probabilities
            a2 = probs[:, 2] * 2 + probs[:, 1]
            delta = abs(dose - a2)
            self.assertTrue(np.nanmax(delta) < 2.5e-7)

    def test_v11_format(self):
        """Can read v1.1 format bgen from S3."""
        path = s3_url("example.v11.bgen")
        with BgenReader(path) as bfile:
            for var, g in zip(bfile, self.gen_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))

    def test_different_bit_depths(self):
        """Can read bgen files with different bit depths from S3."""
        for bits in [8, 16, 32]:
            path = s3_url(f"example.{bits}bits.bgen")
            with BgenReader(path) as bfile:
                var = next(bfile)
                g = self.gen_data[0]
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bits))


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3ComplexBgen(unittest.TestCase):
    """Tests for complex bgen files read from S3."""

    @classmethod
    def setUpClass(cls):
        cls.vcf_data = load_vcf_data()
        cls.haps_data = load_haps_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        setup_s3_env()

    def test_complex_file(self):
        """Can read complex bgen from S3."""
        path = s3_url("complex.bgen")
        with BgenReader(path) as bfile:
            for var, g in zip(bfile, self.vcf_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))
                self.assertTrue(all(x == y for x, y in zip(g.ploidy, var.ploidy)))

    def test_haplotypes(self):
        """Can read haplotype bgen from S3."""
        path = s3_url("haplotypes.bgen")
        with BgenReader(path) as bfile:
            for var, g in zip(bfile, self.haps_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestS3ErrorHandling(unittest.TestCase):
    """Tests for S3 error handling."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        setup_s3_env()

    def test_missing_file_raises(self):
        """Opening a non-existent S3 object raises an error."""
        path = s3_url("nonexistent_file.bgen")
        with self.assertRaises(Exception):
            BgenReader(path)

    def test_context_handler_closes(self):
        """BgenReader context handler works with S3 streams."""
        path = s3_url("example.16bits.zstd.bgen")
        with BgenReader(path) as bfile:
            var = next(bfile)
            self.assertTrue(var.pos > 0)
        # After context exit, accessing data should raise
        with self.assertRaises(ValueError):
            bfile.samples
