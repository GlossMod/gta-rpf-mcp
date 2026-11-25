"""Real RPF7 jobs for streamed components and a DLC-supplied Menyoo inventory."""
import unittest
import xml.etree.ElementTree as ET

from gta_rpf_mcp.backend import ArchiveBackend
from gta_rpf_mcp.gta_xml import PED_META
from gta_rpf_mcp import rpf7
from test_python_backend import open_rpf_fixture, resource_fixture
import test_service as fixtures


class ModelJobTests(unittest.TestCase):
    setUp = fixtures.JobTests.setUp
    new_job = fixtures.JobTests.new_job
    work = fixtures.JobTests.work
    input = fixtures.JobTests.input
    file = fixtures.JobTests.file
    asset = fixtures.JobTests.asset

    def test_menyoo_reads_an_existing_nested_dlc(self):
        cars = open_rpf_fixture({'addon.yft': b'x', 'addon.ytd': b'y'})
        dlc = open_rpf_fixture({'setup2.xml': fixtures.SETUP.format('testaddon').encode(), 'x64/vehicles.rpf': cars})
        contribution = {'kind': 'dlc', 'packName': 'testaddon', 'source': self.file('dlc', dlc)}
        result = self.work(fixtures.target('menyoo', None, contribution), factory=ArchiveBackend)
        self.assertEqual(result['status'], 'ok', result)
        self.assertIn('modelName="addon"', (self.job / 'output/menyoo.xml').read_text(encoding='utf-8'))

    def test_streamed_and_nonstreamed_peds_reopen_from_created_dlc(self):
        raw = resource_fixture()
        simple = {'kind': 'ped', 'modelName': 'plain', 'sex': 'Male', 'streamed': False,
                  'files': [self.asset('plain.ydd', raw)]}
        streamed = {'kind': 'ped', 'modelName': 'streamed', 'sex': 'Female', 'streamed': True,
                    'files': [self.asset('streamed.yft', raw), self.asset('streamed/uppr_000_u.ydd', raw)]}
        result = self.work(fixtures.target('gmm', None, simple, streamed), factory=ArchiveBackend)
        self.assertEqual(result['status'], 'ok', result)
        built = self.job / 'output/gmm.rpf'
        for name in ('plain.ydd', 'streamed.yft', 'streamed/uppr_000_u.ydd'):
            self.assertEqual(rpf7.read_entry(built, f'x64/peds.rpf/{name}'), raw)
        peds = ET.fromstring(rpf7.read_entry(built, PED_META))
        observed = {i.findtext('Name'): i.find('IsStreamedGfx').get('value') for i in peds.findall('InitDatas/Item')}
        self.assertEqual(observed, {'plain': 'false', 'streamed': 'true'})
