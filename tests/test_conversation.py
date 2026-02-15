import os
import re
import unittest

import tools.conversation


_FILES = [
    '_NPC.EMC', '_STARTUP.EMC',
    'ALCHEMY.EMC', 'ALGAE.EMC', 'ALTAR.EMC', 'ARCH.EMC',
    'BALCONY.EMC', 'BELROOM.EMC', 'BONKBG.EMC', 'BRIDGE.EMC', 'BRINS.EMC', 'BROKEN.EMC', 'BURN.EMC',
    'CASTLE.EMC', 'CATACOM.EMC', 'CAVE.EMC', 'CAVEB.EMC', 'CGATE.EMC', 'CHASM.EMC', 'CLIFF.EMC', 'CLIFFB.EMC',
    'DARMS.EMC', 'DEAD.EMC', 'DNSTAIR.EMC', 'DRAGON.EMC',
    'EDGE.EMC', 'EDGEB.EMC', 'EMCAV.EMC', 'ENTER.EMC', 'EXTGEM.EMC', 'EXTHEAL.EMC', 'EXTPOT.EMC', 'EXTSPEL.EMC',
    'FALLS.EMC', 'FESTSTH.EMC', 'FGOWEST.EMC', 'FNORTH.EMC', 'FORESTA.EMC', 'FORESTB.EMC', 'FORESTC.EMC', 'FOUNTN.EMC', 'FOYER.EMC', 'FSOUTH.EMC', 'FSOUTHB.EMC', 'FWSTSTH.EMC',
    'GATECV.EMC', 'GEM.EMC', 'GEMCUT.EMC', 'GENCAVB.EMC', 'GENHALL.EMC', 'GEN_CAV.EMC', 'GLADE.EMC', 'GRAVE.EMC', 'GRTHALL.EMC',
    'HEALER.EMC',
    'KITCHEN.EMC', 'KYRAGEM.EMC',
    'LAGOON.EMC', 'LANDING.EMC', 'LAVA.EMC', 'LEPHOLE.EMC', 'LIBRARY.EMC',
    'MIX.EMC', 'MOONCAV.EMC',
    'NCLIFF.EMC', 'NCLIFFB.EMC', 'NWCLIFB.EMC', 'NWCLIFF.EMC',
    'OAKS.EMC',
    'PLATEAU.EMC', 'PLTCAVE.EMC', 'POTION.EMC',
    'RUBY.EMC',
    'SICKWIL.EMC', 'SONG.EMC', 'SORROW.EMC', 'SPELL.EMC', 'SPRING.EMC', 'SQUARE.EMC', 'STUMP.EMC',
    'TEMPLE.EMC', 'TRUNK.EMC',
    'UPSTAIR.EMC',
    'WELL.EMC', 'WILLOW.EMC', 'WISE.EMC',
    'XEDGE.EMC', 'XEDGEB.EMC', 'XEDGEC.EMC',
    'ZROCK.EMC', 'ZROCKB.EMC',
]


class TestConversation(unittest.TestCase):
    def test_read(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in _FILES:
            with self.subTest(file=name):
                emc_path = os.path.join(root, 'original', name)
                kyra_path = os.path.join(root, 'conversations', name + '.kyra')

                with open(emc_path, 'rb') as f:
                    emc_bytes = f.read()

                expected = tools.conversation.decompile(emc_bytes, name=name)

                if not os.path.exists(kyra_path):
                    self.fail(f'Missing conversation source file: {kyra_path}.')

                with open(kyra_path, 'r', encoding='utf-8') as f:
                    actual = f.read()

                self.assertEqual(expected, actual)

    def test_write(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in _FILES:
            with self.subTest(file=name):
                emc_path = os.path.join(root, 'original', name)
                kyra_path = os.path.join(root, 'conversations', name + '.kyra')

                with open(emc_path, 'rb') as f:
                    original_bytes = f.read()

                with open(kyra_path, 'r', encoding='utf-8') as f:
                    src = f.read()

                rebuilt = tools.conversation.compile(src)
                self.assertEqual(original_bytes, rebuilt)

    def test_no_unreferenced_synthetic_labels(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        label_def_re = re.compile(r'^\s*label\s+(label_\d+)\s*$')
        label_ref_re = re.compile(r'\b(label_\d+)\b')

        for name in _FILES:
            with self.subTest(file=name):
                kyra_path = os.path.join(root, 'conversations', name + '.kyra')
                with open(kyra_path, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()

                defined: set[str] = set()
                referenced: set[str] = set()

                for line in lines:
                    m = label_def_re.match(line)
                    if m:
                        defined.add(m.group(1))
                        continue
                    referenced.update(label_ref_re.findall(line))

                # `label_NNN` is purely synthetic; if it exists, it must be used.
                unused = sorted(defined - referenced)
                self.assertEqual(unused, [], f'Unreferenced synthetic labels in {name}: {unused}')
