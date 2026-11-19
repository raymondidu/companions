import unittest
from unittest.mock import patch
from types import SimpleNamespace
import pandas as pd
import companion_tournament as t
class GuardTests(unittest.TestCase):
 def test_all_frames_both_directions(self):
  for direction in ['LONG','SHORT']:
   for idx in range(3):
    frames=[pd.DataFrame({'low':[80.]*60,'high':[120.]*60}) for _ in range(3)]
    frames[idx]['low' if direction=='SHORT' else 'high']=100.
    candidate=SimpleNamespace(reference_price=100.,entry_atr=4.,direction=direction)
    with patch.object(t,'_evaluate_profile_unchecked',return_value=(candidate,'ACCEPTED')):
     result,reason=t.evaluate_profile(None,None,*frames,99.9,100.1)
     self.assertIsNone(result);self.assertEqual(reason,'SELL_INTO_SUPPORT' if direction=='SHORT' else 'BUY_INTO_RESISTANCE')
 def test_clear_space_passes_and_invalid_blocks(self):
  frames=[pd.DataFrame({'low':[80.]*60,'high':[120.]*60}) for _ in range(3)]
  candidate=SimpleNamespace(reference_price=100.,entry_atr=4.,direction='SHORT')
  with patch.object(t,'_evaluate_profile_unchecked',return_value=(candidate,'ACCEPTED')):
   self.assertIs(t.evaluate_profile(None,None,*frames,99.9,100.1)[0],candidate)
   frames[1]['low']=float('nan')
   self.assertEqual(t.evaluate_profile(None,None,*frames,99.9,100.1)[1],'SUPPORT_RESISTANCE_DATA_INVALID')
if __name__=='__main__':unittest.main()
