# This is an port and update of the original smoketest.py
import json
import os
import random
import unittest
from functools import reduce
from operator import and_
from shutil import copyfile
from unittest import TestCase

from benji.blockuidhistory import BlockUidHistory
from benji.logging import logger
from benji.scripts.benji import hints_from_rbd_diff
from benji.tests.testcase import BenjiTestCaseBase

kB = 1024
MB = kB * 1024
GB = MB * 1024


class SmokeTestCase(BenjiTestCaseBase):

    @staticmethod
    def patch(filename, offset, data=None):
        """ write data into a file at offset """
        if not os.path.exists(filename):
            open(filename, 'wb').close()
        with open(filename, 'r+b') as f:
            f.seek(offset)
            f.write(data)

    @staticmethod
    def same(file1, file2):
        """ returns False if files differ, True if they are the same """
        with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
            d1 = f1.read()
            d2 = f2.read()
        return d1 == d2

    def test_sanity(self):
        file1 = os.path.join(self.testpath.path, 'file1')
        file2 = os.path.join(self.testpath.path, 'file2')
        with open(file1, 'w') as f1, open(file2, 'w') as f2:
            f1.write('hallo' * 100)
            f2.write('huhu' * 100)
        self.assertTrue(self.same(file1, file1))
        self.assertFalse(self.same(file1, file2))
        os.unlink(file1)
        os.unlink(file2)

    def test(self):
        testpath = self.testpath.path
        base_version_uid = None
        version_uids = []
        old_size = 0
        init_database = True
        image_filename = os.path.join(testpath, 'image')
        block_size = random.sample({512, 1024, 2048, 4096}, 1)[0]
        scrub_history = BlockUidHistory()
        deep_scrub_history = BlockUidHistory()
        storage_name = 's1'
        for i in range(1, 100):
            logger.debug('Run {}'.format(i + 1))
            hints = []
            if not os.path.exists(image_filename):
                open(image_filename, 'wb').close()
            if old_size and random.randint(0, 10) == 0:  # every 10th time or so do not apply any changes.
                size = old_size
            else:
                size = 32 * 4 * kB + random.randint(-4 * kB, 4 * kB)
                for j in range(random.randint(0, 10)):  # up to 10 changes
                    if random.randint(0, 1):
                        patch_size = random.randint(0, 4 * kB)
                        data = self.random_bytes(patch_size)
                        exists = "true"
                    else:
                        patch_size = random.randint(0, 4 * 4 * kB)  # we want full blocks sometimes
                        data = b'\0' * patch_size
                        exists = "false"
                    offset = random.randint(0, size - patch_size - 1)
                    logger.debug('Applied change at {}({}):{}, exists {}'.format(offset, int(offset / 4096), patch_size,
                                                                                 exists))
                    self.patch(image_filename, offset, data)
                    hints.append({'offset': offset, 'length': patch_size, 'exists': exists})

            # truncate?
            with open(image_filename, 'r+b') as f:
                f.truncate(size)

            if old_size and size > old_size:
                patch_size = size - old_size + 1
                offset = old_size - 1
                logger.debug('Image got bigger at {}({}):{}'.format(offset, int(offset / 4096), patch_size))
                hints.append({'offset': offset, 'length': patch_size, 'exists': 'true'})

            old_size = size

            copyfile(image_filename, '{}.{}'.format(image_filename, i + 1))

            logger.debug('Applied {} changes, size is {}.'.format(len(hints), size))
            with open(os.path.join(testpath, 'hints'), 'w') as f:
                f.write(json.dumps(hints))

            benji_obj = self.benjiOpen(init_database=init_database, block_size=block_size)
            init_database = False
            with open(os.path.join(testpath, 'hints')) as hints:
                version = benji_obj.backup(
                    version_name='data-backup',
                    version_snapshot_name='snapshot-name',
                    source='file://' + image_filename,
                    hints=hints_from_rbd_diff(hints.read()) if base_version_uid else None,
                    base_version_uid=base_version_uid,
                    storage_name=storage_name)
                # Don't keep a reference to version because we're closing the SQLAlchemy session
                version_uid = version.uid
            benji_obj.close()
            version_uids.append(version_uid)
            logger.debug('Backup successful')

            benji_obj = self.benjiOpen()
            benji_obj.add_label(version_uid, 'label-1', 'value-1')
            benji_obj.add_label(version_uid, 'label-2', 'value-2')
            benji_obj.close()
            logger.debug('Labeling of version successful')

            benji_obj = self.benjiOpen()
            benji_obj.rm(version_uid, force=True, keep_metadata_backup=True)
            benji_obj.close()
            logger.debug('Removal of version successful')

            benji_obj = self.benjiOpen()
            benji_obj.metadata_restore([version_uid], storage_name)
            benji_obj.close()
            logger.debug('Restore of version successful')

            benji_obj = self.benjiOpen()
            blocks = benji_obj._database_backend.get_blocks_by_version(version_uid)
            self.assertEqual(list(range(len(blocks))), sorted([block.id for block in blocks]))
            self.assertTrue(len(blocks) > 0)
            if len(blocks) > 1:
                self.assertTrue(reduce(and_, [block.size == block_size for block in blocks[:-1]]))
            benji_obj.close()
            logger.debug('Block list successful')

            benji_obj = self.benjiOpen()
            versions = benji_obj.ls()
            self.assertEqual(set(), set([version.uid for version in versions]) ^ set(version_uids))
            self.assertTrue(reduce(and_, [version.name == 'data-backup' for version in versions]))
            self.assertTrue(reduce(and_, [version.snapshot_name == 'snapshot-name' for version in versions]))
            self.assertTrue(reduce(and_, [version.block_size == block_size for version in versions]))
            self.assertTrue(reduce(and_, [version.size > 0 for version in versions]))
            benji_obj.close()
            logger.debug('Version list successful')

            benji_obj = self.benjiOpen()
            benji_obj.scrub(version_uid)
            benji_obj.close()
            logger.debug('Scrub successful')

            benji_obj = self.benjiOpen()
            benji_obj.deep_scrub(version_uid)
            benji_obj.close()
            logger.debug('Deep scrub successful')

            benji_obj = self.benjiOpen()
            benji_obj.deep_scrub(version_uid, 'file://' + image_filename)
            benji_obj.close()
            logger.debug('Deep scrub with source successful')

            benji_obj = self.benjiOpen()
            benji_obj.scrub(version_uid, history=scrub_history)
            benji_obj.close()
            logger.debug('Scrub with history successful')

            benji_obj = self.benjiOpen()
            benji_obj.deep_scrub(version_uid, history=deep_scrub_history)
            benji_obj.close()
            logger.debug('Deep scrub with history successful')

            restore_filename_1 = os.path.join(testpath, 'restore.{}'.format(i + 1))
            restore_filename_2 = os.path.join(testpath, 'restore-mdl.{}'.format(i + 1))
            benji_obj = self.benjiOpen()
            benji_obj.restore(version_uid, 'file://' + restore_filename_1, sparse=False, force=False)
            benji_obj.close()
            self.assertTrue(self.same(image_filename, restore_filename_1))
            logger.debug('Restore successful')

            benji_obj = self.benjiOpen(in_memory_database=True)
            benji_obj.metadata_restore([version_uid], storage_name)
            benji_obj.restore(version_uid, 'file://' + restore_filename_2, sparse=False, force=False)
            benji_obj.close()
            self.assertTrue(self.same(image_filename, restore_filename_2))
            logger.debug('Metadata-backend-less restore successful')
            base_version_uid = version_uid

            # delete old versions
            if len(version_uids) > 10:
                benji_obj = self.benjiOpen()
                dismissed_versions = benji_obj.enforce_retention_policy('name=="data-backup"', 'latest10,hours24,days30')
                for dismissed_version in dismissed_versions:
                    version_uids.remove(dismissed_version.uid)
                benji_obj.close()

            if (i % 7) == 0:
                benji_obj = self.benjiOpen()
                benji_obj.cleanup(dt=0)
                benji_obj.close()
            if (i % 13) == 0:
                scrub_history = BlockUidHistory()
                deep_scrub_history = BlockUidHistory()
            if (i % 23) == 0:
                base_version_uid = None
                if storage_name == 's1':
                    storage_name = 's2'
                else:
                    storage_name = 's1'


class SmokeTestCaseSQLLite_File(SmokeTestCase, TestCase):

    CONFIG = """
            configurationVersion: '1'
            processName: benji
            logFile: /dev/stderr
            hashFunction: BLAKE2b,digest_bits=256
            blockSize: 4096
            ios:
            - name: file
              module: file
              configuration:
                simultaneousReads: 2
            defaultStorage: s1
            storages:
            - name: s1
              storageId: 1
              module: file
              configuration:
                path: {testpath}/data
                consistencyCheckWrites: True
                simultaneousWrites: 5
                simultaneousReads: 5                    
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
            - name: s2
              storageId: 2
              module: file
              configuration:
                path: {testpath}/data-2
                consistencyCheckWrites: True
                simultaneousWrites: 5
                simultaneousReads: 5                    
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123        
            transforms:
            - name: zstd
              module: zstd
              configuration:
                level: 1
            - name: k1
              module: aes_256_gcm
              configuration:
                kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                kdfIterations: 20000
                password: "this is a very secret password"
            databaseEngine: sqlite:///{testpath}/benji.sqlite
            """


@unittest.skipIf(os.environ.get('UNITTEST_SKIP_POSTGRESQL', False), 'No PostgreSQL setup available.')
class SmokeTestCasePostgreSQL_File(SmokeTestCase, TestCase):

    CONFIG = """
            configurationVersion: '1'
            processName: benji
            logFile: /dev/stderr
            hashFunction: SHA256
            blockSize: 4096
            ios:
            - name: file
              module: file
              configuration:
                simultaneousReads: 2
            defaultStorage: s1
            storages:
            - name: s1
              storageId: 1
              module: file
              configuration:
                path: {testpath}/data
                consistencyCheckWrites: True
                simultaneousWrites: 5
                simultaneousReads: 5                    
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
            - name: s2
              storageId: 2
              module: file
              configuration:
                path: {testpath}/data-2
                consistencyCheckWrites: True
                simultaneousWrites: 5
                simultaneousReads: 5                    
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123        
            transforms:
            - name: zstd
              module: zstd
              configuration:
                level: 1
            - name: k1
              module: aes_256_gcm
              configuration:
                kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                kdfIterations: 20000
                password: "this is a very secret password"
            databaseEngine: postgresql://benji:verysecret@localhost:15432/benji
            """


@unittest.skipIf(
    os.environ.get('UNITTEST_SKIP_POSTGRESQL', False) or os.environ.get('UNITTEST_SKIP_S3', False),
    'No PostgreSQL or S3 setup available.')
class SmokeTestCasePostgreSQL_S3(SmokeTestCase, TestCase):

    CONFIG = """
            configurationVersion: '1'
            processName: benji
            logFile: /dev/stderr
            hashFunction: SHA512
            blockSize: 4096
            ios:
            - name: file
              module: file
              configuration:
                simultaneousReads: 2
            defaultStorage: s1
            storages:
            - name: s1
              storageId: 1
              module: s3
              configuration:
                awsAccessKeyId: minio
                awsSecretAccessKey: minio123
                endpointUrl: http://127.0.0.1:9901/
                bucketName: benji
                multiDelete: true
                addressingStyle: path
                disableEncodingType: false
                consistencyCheckWrites: True                 
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
            - name: s2
              storageId: 2
              module: s3
              configuration:
                awsAccessKeyId: minio
                awsSecretAccessKey: minio123
                endpointUrl: http://127.0.0.1:9901/
                bucketName: benji-2
                multiDelete: true
                addressingStyle: path
                disableEncodingType: false
                consistencyCheckWrites: True                 
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123        
            transforms:
            - name: zstd
              module: zstd
              configuration:
                level: 1
            - name: k1
              module: aes_256_gcm
              configuration:
                kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                kdfIterations: 20000
                password: "this is a very secret password"
            databaseEngine: postgresql://benji:verysecret@localhost:15432/benji
            """


@unittest.skipIf(
    os.environ.get('UNITTEST_SKIP_POSTGRESQL', False) or os.environ.get('UNITTEST_SKIP_S3', False),
    'No PostgreSQL or S3 setup available.')
class SmokeTestCasePostgreSQL_S3_ReadCache(SmokeTestCase, TestCase):

    CONFIG = """
            configurationVersion: '1'
            processName: benji
            logFile: /dev/stderr
            hashFunction: SHA224
            blockSize: 4096
            ios:
            - name: file
              module: file
              configuration:
                simultaneousReads: 2
            defaultStorage: s1
            storages:
            - name: s1
              storageId: 1
              module: s3
              configuration:
                awsAccessKeyId: minio
                awsSecretAccessKey: minio123
                endpointUrl: http://127.0.0.1:9901/
                bucketName: benji
                multiDelete: true
                addressingStyle: path
                disableEncodingType: false
                consistencyCheckWrites: True                 
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
                readCache:
                  directory: {testpath}/read-cache
                  maximumSize: 16777216
                  shards: 8
            - name: s2
              storageId: 2
              module: s3
              configuration:
                awsAccessKeyId: minio
                awsSecretAccessKey: minio123
                endpointUrl: http://127.0.0.1:9901/
                bucketName: benji-2
                multiDelete: true
                addressingStyle: path
                disableEncodingType: false
                consistencyCheckWrites: True                 
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
                readCache: 
                  directory: {testpath}/read-cache-2
                  maximumSize: 16777216
                  shards: 8
            transforms:
            - name: zstd
              module: zstd
              configuration:
                level: 1
            - name: k1
              module: aes_256_gcm
              configuration:
                kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                kdfIterations: 20000
                password: "this is a very secret password"
            databaseEngine: postgresql://benji:verysecret@localhost:15432/benji              
            """


@unittest.skipIf(
    os.environ.get('UNITTEST_SKIP_POSTGRESQL', False) or os.environ.get('UNITTEST_SKIP_B2', False),
    'No PostgreSQL or B2 setup available.')
class SmokeTestCasePostgreSQL_B2(SmokeTestCase):

    CONFIG = """
            configurationVersion: '1'
            processName: benji
            logFile: /dev/stderr
            hashFunction: SHA512
            blockSize: 4096
            ios:
            - name: file
              module: file
              configuration:
                simultaneousReads: 2
            defaultStorage: s1
            storages:
            - name: s1
              storageId: 1
              module: b2
              configuration:
                accountIdFile: ../../../.b2-account-id.txt
                applicationKeyFile: ../../../.b2-application-key.txt
                bucketName: elemental-backy2-test
                accountInfoFile: {testpath}/b2_account_info
                writeObjectAttempts: 3
                readObjectAttempts: 3
                uploadAttempts: 5
                consistencyCheckWrites: True
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123
            - name: s2
              storageId: 2
              module: b2
              configuration:
                accountIdFile: ../../../.b2-account-id.txt
                applicationKeyFile: ../../../.b2-application-key.txt
                bucketName: elemental-backy2-legolas
                accountInfoFile: {testpath}/b2_account_info
                writeObjectAttempts: 3
                readObjectAttempts: 3
                uploadAttempts: 5
                consistencyCheckWrites: True              
                activeTransforms:
                  - zstd
                  - k1
                hmac:
                  kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                  kdfIterations: 1000
                  password: Hallo123        
            transforms:
            - name: zstd
              module: zstd
              configuration:
                level: 1
            - name: k1
              module: aes_256_gcm
              configuration:
                kdfSalt: BBiZ+lIVSefMCdE4eOPX211n/04KY1M4c2SM/9XHUcA=
                kdfIterations: 20000
                password: "this is a very secret password"
            databaseEngine: postgresql://benji:verysecret@localhost:15432/benji              
            """
