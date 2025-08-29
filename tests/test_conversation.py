import json
import os
import unittest

import tools.conversation


class TestConversation(unittest.TestCase):
    def test_read(self):
        path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = [
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
        for file in files:
            with self.subTest(file=file):
                with open(os.path.join(path, 'original', file), 'rb') as f:
                    data = f.read()
                content = tools.conversation.decode(data)
                with open(os.path.join(path, 'conversations', file + '.json'), 'r') as f:
                    self.assertEqual({'order': content[0], 'text': content[1], 'data': content[2]}, json.load(f))
