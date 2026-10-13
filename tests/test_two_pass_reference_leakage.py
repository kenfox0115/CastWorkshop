import unittest
from worker.image_prompts import build_sequential_image_prompts, _parse_sequential_intent

class ReferenceLeakageTests(unittest.TestCase):
    metadata={'Juno': {'gender':'female','concept':'woman with scars and permanent piercings'},
              'Amy': {'gender':'female','concept':'woman with tattoos, denim vest, graphic T-shirt, chains and jewelry'}}

    def prompts(self, action='Both sitting at a bar', request='both topless', names=('Juno','Amy')):
        return build_sequential_image_prompts(action,*names,latest_request=request,
                    adult_mode=True,character_metadata=self.metadata)

    def test_raw_global_request_is_not_appended(self):
        for action in ('both topless', 'Juno and Amy are both sitting at a bar topless',
                       'Requested edit: Both sitting at a bar topless'):
            first,second=self.prompts(action)
            self.assertNotIn('both',first.lower())
            self.assertNotIn('Amy',first)
            self.assertNotIn('Requested edit:',first)
            self.assertNotIn('Requested edit:',second)
            self.assertNotIn('both topless',second.lower())

    def test_structured_intent_excludes_other_wardrobe(self):
        intent=_parse_sequential_intent('Juno is wearing a dress. Amy is fully nude.', 'Juno','Amy')
        self.assertIn('Juno is wearing a dress',intent.first.pose_action)
        self.assertNotIn('Amy',intent.first.pose_action)
        self.assertNotIn('fully nude',intent.first.wardrobe)
        self.assertIn('Amy wears no clothing or lingerie',intent.second.wardrobe)
        self.assertNotIn('dress',intent.second.pose_action)

    def test_permanent_identity_features_are_preserved_when_present(self):
        first,second=self.prompts()
        for prompt in (first,second):
            for feature in ('facial identity and structure','hair color, style and length','skin tone',
                            'body type and build','stable age appearance','distinctive permanent physical features',
                            'tattoos','scars','birthmarks','permanent piercings','distinctive moles','when present'):
                self.assertIn(feature,prompt)

    def test_reference_transients_are_not_identity_traits(self):
        first,second=self.prompts()
        for prompt in (first,second):
            identity=prompt.split('only to preserve ',1)[1].split('when present.',1)[0]
            for transient in ('clothing','jewelry','chains','pose','background','facial expression','lighting'):
                self.assertNotIn(transient,identity.lower())
            self.assertIn('Transient visual state comes from the request',prompt)
            self.assertIn('temporary makeup or styling follow the requested final image',prompt)

    def test_freeze_is_explicit_and_spatial_only(self):
        _,second=self.prompts()
        self.assertIn('Juno is already correct',second)
        self.assertIn('Preserve Juno exactly as established in image 1',second)
        self.assertIn('identity, face, hair, permanent physical features, body, position, pose, expression, and wardrobe state',second)
        self.assertIn('Do not restyle, redress, replace, reinterpret, or transfer attributes onto Juno',second)
        self.assertIn('Only make minimal spatial adjustments if absolutely necessary',second)
        self.assertIn("without changing Juno's established appearance",second)
        self.assertNotIn('adjust their pose',second)
        self.assertNotIn('Make only minimal changes',second)

    def test_global_transfer_ban_covers_reference_outfit_and_accessories(self):
        first,second=self.prompts()
        self.assertIn("Image 2 provides Amy's identity only",second)
        self.assertIn('Do not copy, transfer, reuse, or apply clothing, accessories, jewelry, chains',second)
        self.assertIn('from image 2 to Juno, the environment, or any other part of the final image',second)
        self.assertIn('reference-only and must not appear anywhere in the final image unless explicitly requested',second)
        for prompt in (first,second):
            self.assertNotIn('denim vest',prompt)
            self.assertNotIn('graphic T-shirt',prompt)

    def test_explicit_requested_accessory_is_not_lost(self):
        first,second=self.prompts('Juno is sitting at a bar. Amy is sitting at the bar wearing a blue shirt and jewelry.',request=None)
        self.assertNotIn('wearing a blue shirt',first)
        self.assertIn('Amy is sitting at the bar wearing a blue shirt and jewelry',second)
        self.assertIn('unless explicitly requested',second)

    def test_swapping_preserves_reference_roles_and_freeze_ownership(self):
        for names in (('Juno','Amy'),('Amy','Juno')):
            first,second=self.prompts(names=names)
            a,b=names
            self.assertNotIn(b,first)
            self.assertIn(f"Use image 2 only to preserve {a}'s facial identity",first)
            self.assertIn(f'{a} is already correct',second)
            self.assertIn(f"Image 2 provides {b}'s identity only",second)
            self.assertIn(f'from image 2 to {a}, the environment',second)
            self.assertNotIn(f'{b} is already correct',second)
