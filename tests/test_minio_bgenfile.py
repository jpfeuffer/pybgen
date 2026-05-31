"""Tests that mirror existing tests but with data hosted in a Minio container.

These tests download test data from a Minio (S3-compatible) object store,
then run the same validations as the local-file-based tests. This verifies
that pybgen works correctly with files retrieved from object storage.

The tests are skipped if no Minio service is available.
"""

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bgen import BgenReader, BgenWriter

from tests.utils import (
    load_gen_data,
    load_vcf_data,
    load_haps_data,
    arrays_equal,
    epsilon,
)
from tests.minio_utils import (
    get_minio_client,
    is_minio_available,
    upload_test_data,
    download_all_test_data,
    download_file,
    MINIO_BUCKET,
)

SKIP_REASON = "Minio service not available"


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestMinioBgenReader(unittest.TestCase):
    """Tests for BgenReader using files downloaded from Minio."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.client = get_minio_client()
        # Upload test data to Minio
        upload_test_data(cls.client)
        # Download all test data from Minio to a temp directory
        cls.tmpdir = tempfile.mkdtemp(prefix="minio_test_")
        download_all_test_data(cls.client, cls.tmpdir)
        cls.folder = Path(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_context_handler_closed_bgen_samples(self):
        """No samples available from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile.samples) > 0)
        with self.assertRaises(ValueError):
            bfile.samples

    def test_context_handler_closed_bgen_varids(self):
        """No varids available from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile.varids()) > 0)
        with self.assertRaises(ValueError):
            bfile.varids()

    def test_context_handler_closed_bgen_rsids(self):
        """No rsids available from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile.rsids()) > 0)
        with self.assertRaises(ValueError):
            bfile.rsids()

    def test_context_handler_closed_bgen_positions(self):
        """No positions available from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile.positions()) > 0)
        with self.assertRaises(ValueError):
            bfile.positions()

    def test_context_handler_closed_bgen_length(self):
        """Error raised if accessing length of exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile) > 0)
        with self.assertRaises(ValueError):
            len(bfile)

    def test_context_handler_closed_bgen_slice(self):
        """Error raised if slicing variant from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile) > 0)
        with self.assertRaises(ValueError):
            var = bfile[0]

    def test_context_handler_closed_bgen_at_position(self):
        """Error raised if getting variant at position from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile) > 0)
        with self.assertRaises(ValueError):
            var = bfile.at_position(100)

    def test_context_handler_closed_bgen_with_rsid(self):
        """Error raised if getting variant with rsid from exited BgenReader."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            self.assertTrue(len(bfile) > 0)
        with self.assertRaises(ValueError):
            var = bfile.with_rsid("rs111")

    def test_context_handler_variant_data_not_loaded(self):
        """Error raised if we try to access variant data after closing BgenFile."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            var = next(bfile)
        with self.assertRaises(ValueError):
            var.minor_allele_dosage

    def test_context_handler_variant_data_loaded(self):
        """No error raised for variant from closed BgenReader, IF data is already loaded."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            var = next(bfile)
            var.minor_allele_dosage  # load data while file still open
        dose = var.minor_allele_dosage
        self.assertTrue(isinstance(dose, np.ndarray))

    def test_fetch(self):
        """Can fetch variants within a genomic region."""
        bfile = BgenReader(self.folder / "example.16bits.bgen")
        self.assertTrue(
            bfile._check_for_index(str(self.folder / "example.16bits.bgen"))
        )
        self.assertTrue(list(bfile.fetch("02")) == [])

    def test_fetch_whole_chrom(self):
        """Fetching just with chrom gives all variants on chromosome."""
        chrom = "01"
        bfile = BgenReader(self.folder / "example.16bits.bgen")
        sortkey = lambda x: (x.chrom, x.pos)
        for x, y in zip(
            sorted(bfile.fetch(chrom), key=sortkey),
            sorted(self.gen_data, key=sortkey),
        ):
            self.assertEqual(x.rsid, y.rsid)
            self.assertEqual(x.chrom, y.chrom)
            self.assertEqual(x.pos, y.pos)

    def test_fetch_after_position(self):
        """Fetching variants with chrom and start gives all variants after pos."""
        chrom, start = "01", 5000
        bfile = BgenReader(self.folder / "example.16bits.bgen")
        sortkey = lambda x: (x.chrom, x.pos)
        gen_vars = [x for x in sorted(self.gen_data, key=sortkey) if start <= x.pos]
        for x, y in zip(sorted(bfile.fetch(chrom, start), key=sortkey), gen_vars):
            self.assertEqual(x.rsid, y.rsid)
            self.assertEqual(x.chrom, y.chrom)
            self.assertEqual(x.pos, y.pos)

    def test_fetch_in_region(self):
        """Fetching variants with chrom, start, stop gives variants in region."""
        chrom, start, stop = "01", 5000, 50000
        bfile = BgenReader(self.folder / "example.16bits.bgen")
        sortkey = lambda x: (x.chrom, x.pos)
        gen_vars = [
            x for x in sorted(self.gen_data, key=sortkey) if start <= x.pos <= stop
        ]
        for x, y in zip(
            sorted(bfile.fetch(chrom, start, stop), key=sortkey), gen_vars
        ):
            self.assertEqual(x.rsid, y.rsid)
            self.assertEqual(x.chrom, y.chrom)
            self.assertEqual(x.pos, y.pos)
        self.assertEqual(list(bfile.fetch(chrom, start * 1000, stop * 1000)), [])


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestMinioExampleBgens(unittest.TestCase):
    """Tests for loading example bgen files from Minio."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.vcf_data = load_vcf_data()
        cls.haps_data = load_haps_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        cls.tmpdir = tempfile.mkdtemp(prefix="minio_test_")
        download_all_test_data(cls.client, cls.tmpdir)
        cls.folder = Path(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_load_example_genotypes_bit_depths(self):
        """Check parsing genotypes from example files with different bit depths."""
        for path in self.folder.glob("example.*bits.bgen"):
            bit_depth = int(path.stem.split(".")[1].strip("bits"))
            bfile = BgenReader(str(path))
            for var, g in zip(bfile, self.gen_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bit_depth))

    def test_zstd_compressed(self):
        """Check we can parse genotypes from zstd compressed geno probabilities."""
        path = self.folder / "example.16bits.zstd.bgen"
        bfile = BgenReader(str(path))
        for var, g in zip(bfile, self.gen_data):
            self.assertEqual(g, var)
            self.assertTrue(arrays_equal(g.probabilities, var.probabilities, 16))

    def test_v11(self):
        """Check we can open a bgen in v1.1 format, and parse genotypes correctly."""
        path = self.folder / "example.v11.bgen"
        bfile = BgenReader(str(path))
        bit_depth = 16
        for var, g in zip(bfile, self.gen_data):
            self.assertEqual(g, var)
            self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bit_depth))

    def test_load_haplotypes_bgen(self):
        """Check we can open a bgen with haplotypes, and parse genotypes correctly."""
        path = self.folder / "haplotypes.bgen"
        bfile = BgenReader(str(path))
        bit_depth = 16
        for var, g in zip(bfile, self.haps_data):
            self.assertEqual(g, var)
            self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bit_depth))

    def test_load_complex_file(self):
        """Make sure we can open a complex bgen file."""
        path = self.folder / "complex.bgen"
        bfile = BgenReader(path)
        bit_depth = 16
        for var, g in zip(bfile, self.vcf_data):
            self.assertEqual(g, var)
            self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bit_depth))
            self.assertTrue(all(x == y for x, y in zip(g.ploidy, var.ploidy)))

    def test_load_complex_files(self):
        """Make sure we can open the complex bgen files."""
        for path in self.folder.glob("complex.*.bgen"):
            bit_depth = int(path.stem.split(".")[1].strip("bits"))
            bfile = BgenReader(path)
            for var, g in zip(bfile, self.vcf_data):
                self.assertEqual(g, var)
                self.assertTrue(arrays_equal(g.probabilities, var.probabilities, bit_depth))

    def test_load_missing_file(self):
        """Check passing in a path to a missing file fails gracefully."""
        with self.assertRaises(ValueError):
            BgenReader("/zzz/jjj/qqq.bgen")


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestMinioBgenVar(unittest.TestCase):
    """Tests for BgenVar using files downloaded from Minio."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        cls.tmpdir = tempfile.mkdtemp(prefix="minio_test_")
        download_all_test_data(cls.client, cls.tmpdir)
        cls.folder = Path(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_minor_allele_dosage(self):
        """Test we calculate minor_allele_dosage correctly."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path) as bfile:
            for var in bfile:
                dose = var.minor_allele_dosage
                probs = var.probabilities
                a1 = probs[:, 0] * 2 + probs[:, 1]
                a2 = probs[:, 2] * 2 + probs[:, 1]
                recomputed = a2 if np.nansum(a1) >= np.nansum(a2) else a1
                delta = abs(dose - recomputed)
                self.assertTrue(np.nanmax(delta) < 2e-7)

    def test_alt_dosage(self):
        """Test we calculate alt_dosage correctly."""
        path = self.folder / "example.16bits.zstd.bgen"
        with BgenReader(path, delay_parsing=True) as bfile:
            for var in bfile:
                dose = var.alt_dosage
                probs = var.probabilities
                a2 = probs[:, 2] * 2 + probs[:, 1]
                delta = abs(dose - a2)
                self.assertTrue(np.nanmax(delta) < 2.5e-7)

    def test_minor_allele_dosage_fast(self):
        """Test we calculate minor_allele_dosage correctly with the fast path."""
        path = self.folder / "example.8bits.bgen"
        with BgenReader(path) as bfile:
            for var in bfile:
                dose = var.minor_allele_dosage
                probs = var.probabilities
                a1 = probs[:, 0] * 2 + probs[:, 1]
                a2 = probs[:, 2] * 2 + probs[:, 1]
                recomputed = a2 if np.nansum(a1) >= np.nansum(a2) else a1
                delta = abs(dose - recomputed)
                self.assertTrue(np.nanmax(delta) < 3e-7)

    def test_minor_allele_dosage_v11(self):
        """Test we calculate minor_allele_dosage correctly with version 1 bgens."""
        path = self.folder / "example.v11.bgen"
        with BgenReader(path) as bfile:
            for var in bfile:
                dose = var.minor_allele_dosage
                probs = var.probabilities
                a1 = probs[:, 0] * 2 + probs[:, 1]
                a2 = probs[:, 2] * 2 + probs[:, 1]
                recomputed = a2 if np.nansum(a1) >= np.nansum(a2) else a1
                delta = abs(dose - recomputed)
                self.assertTrue(np.nanmax(delta) < 7e-5)


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestMinioBgenIndex(unittest.TestCase):
    """Tests for bgen.index.Index using files downloaded from Minio."""

    @classmethod
    def setUpClass(cls):
        cls.gen_data = load_gen_data()
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        cls.tmpdir = tempfile.mkdtemp(prefix="minio_test_")
        download_all_test_data(cls.client, cls.tmpdir)
        cls.folder = Path(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_index_opens(self):
        """Loads index when available."""
        from bgen.index import Index

        bfile = BgenReader(self.folder / "example.15bits.bgen")
        self.assertFalse(
            bfile._check_for_index(str(self.folder / "example.15bits.bgen"))
        )

        bfile = BgenReader(self.folder / "example.16bits.bgen")
        self.assertTrue(
            bfile._check_for_index(str(self.folder / "example.16bits.bgen"))
        )

    def test_index_fetch(self):
        """Fetches file offsets."""
        from bgen.index import Index

        chrom = "01"
        start = 5000
        stop = 50000

        index = Index(self.folder / "example.16bits.bgen.bgi")
        self.assertTrue(len(list(index.fetch(chrom))) == len(self.gen_data))
        self.assertTrue(len(list(index.fetch("02"))) == 0)
        self.assertTrue(len(list(index.fetch(chrom, start * 100, stop * 100))) == 0)

        chrom_offsets = list(index.fetch(chrom))
        self.assertTrue(len(chrom_offsets) > 0)
        self.assertTrue(len(chrom_offsets) == len(self.gen_data))

        after_pos_offsets = list(index.fetch(chrom, start))
        self.assertTrue(len(after_pos_offsets) > 0)
        self.assertTrue(
            len(after_pos_offsets)
            == len([x for x in self.gen_data if start <= x.pos])
        )

        in_region_offsets = list(index.fetch(chrom, start, stop))
        self.assertTrue(len(in_region_offsets) > 0)
        self.assertTrue(
            len(in_region_offsets)
            == len([x for x in self.gen_data if start <= x.pos <= stop])
        )

        self.assertTrue(len(chrom_offsets) != len(after_pos_offsets))
        self.assertTrue(len(chrom_offsets) != len(in_region_offsets))
        self.assertTrue(len(after_pos_offsets) != len(in_region_offsets))


@unittest.skipUnless(is_minio_available(), SKIP_REASON)
class TestMinioAltDosage(unittest.TestCase):
    """Tests for alt dosage with files downloaded from Minio."""

    @classmethod
    def setUpClass(cls):
        cls.client = get_minio_client()
        upload_test_data(cls.client)
        cls.tmpdir = tempfile.mkdtemp(prefix="minio_test_")
        download_all_test_data(cls.client, cls.tmpdir)
        cls.folder = Path(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_alt_dosage_nonstandard(self):
        """variant.alt_dosage is correct with variable ploidy and with phased data."""
        path = self.folder / "alt_dosage_check.bgen"
        with BgenReader(path) as bfile:
            for variant in bfile:
                dose = variant.alt_dosage
                probs = variant.probabilities
                haploid = variant.ploidy == 1
                alt_dose = np.empty(len(probs))
                if variant.is_phased:
                    alt_dose[~haploid] = probs[~haploid, 1] + probs[~haploid, 3]
                    alt_dose[haploid] = probs[haploid, 1]
                else:
                    alt_dose[~haploid] = 2 * probs[~haploid, 2] + probs[~haploid, 1]
                    alt_dose[haploid] = probs[haploid, 1]
                self.assertTrue((dose >= 0).all())
                self.assertTrue((dose == alt_dose).all())
