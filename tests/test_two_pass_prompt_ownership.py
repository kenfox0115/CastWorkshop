import unittest
from worker.image_prompts import build_sequential_image_prompts

class TwoPassOwnershipTests(unittest.TestCase):
    metadata = {'Molly': {'gender': 'female'}, 'Zara': {'concept': 'adult woman'}}

    def prompts(self, wardrobe=None, scene='Both sitting at a bar'):
        return build_sequential_image_prompts(scene, 'Molly', 'Zara',
            latest_request=wardrobe, adult_mode=True, character_metadata=self.metadata)

    def test_topless_bar_ownership_and_frozen_first_person(self):
        first, second = self.prompts('both topless')
        self.assertIn('Molly', first)
        self.assertNotIn('Zara', first)
        self.assertIn('exactly one adult woman: Molly', first)
        self.assertIn('Do not generate any additional person', first)
        self.assertIn('Molly has a bare upper body', first)
        self.assertIn('no bra, shirt, top, jacket, vest, or other upper-body garment', first)
        self.assertIn('open space beside Molly for a second person', first)
        self.assertNotIn('exactly two', first)
        self.assertIn('Molly is sitting at a bar', first)
        self.assertIn('Image 1 is the established scene containing Molly', second)
        self.assertIn('Preserve the environment, composition, camera position, lighting', second)
        self.assertIn("Preserve Molly exactly as established in image 1, including identity, face, hair, permanent physical features, body, position, pose, expression, and wardrobe state", second)
        self.assertIn('Do not restyle, redress, replace, reinterpret, or transfer attributes onto Molly', second)
        self.assertIn('Only make minimal spatial adjustments if absolutely necessary', second)
        self.assertIn("Use image 2 only to preserve Zara's facial identity", second)
        self.assertIn('Zara has a bare upper body', second)
        self.assertIn("Do not preserve conflicting upper-body clothing from Zara's", second)
        self.assertNotIn('Molly has a bare upper body', second)
        self.assertIn('exactly two adult women: Molly and Zara', second)
        self.assertIn('Keep their identities distinct. Do not blend or duplicate the characters.', second)
        self.assertNotIn('adjust their pose', second)

    def test_all_wardrobe_states_remain_owned_by_their_pass(self):
        for state, concrete in [('topless','bare upper body'), ('fully nude','no clothing or lingerie'),
                                ('lingerie','complete final outfit'), ('both wearing blue shirts','completely replaces conflicting source wardrobe')]:
            with self.subTest(state=state):
                first,second=self.prompts(state)
                self.assertNotIn('Zara',first)
                self.assertIn(concrete,first)
                self.assertIn(concrete,second)
                wardrobe_section=second.rsplit('The final image contains',1)[-1].split('characters.',1)[-1]
                self.assertNotIn('Molly',wardrobe_section)
                self.assertNotIn("Molly's reference images",second)
                self.assertIn('Zara',wardrobe_section)

    def test_no_wardrobe_change_does_not_inject_override(self):
        first,second=self.prompts(scene='Molly is wearing a dress. Zara is wearing a jacket. Both sitting at a bar.')
        self.assertIn('Molly is wearing a dress',first)
        self.assertNotIn('Zara',first)
        self.assertNotIn('jacket',first)
        self.assertNotIn('Both',first)
        self.assertIn('Molly is sitting at a bar',first)
        self.assertIn('Zara is wearing a jacket',second)
        self.assertNotIn('wearing a dress',second)
        for prompt in (first,second):
            self.assertNotIn('conflicting source wardrobe',prompt)
            self.assertNotIn('bare upper',prompt)

    def test_different_wardrobes_are_not_copied_between_people(self):
        first,second=self.prompts(scene='Molly is topless. Zara is wearing lingerie.')
        self.assertIn('Molly has a bare upper body',first)
        self.assertNotIn('lingerie',first)
        self.assertNotIn('bare upper body',second)
        self.assertIn('Zara is wearing lingerie',second)

    def test_funny_conversation_becomes_visible_interaction(self):
        first,second=self.prompts(scene='Both sitting at a bar having a funny conversation')
        self.assertNotIn('Zara',first)
        self.assertIn('amused expression',first)
        self.assertIn('smiling naturally',second)
        self.assertIn('talking',second)
        for prompt in (first,second):
            self.assertNotIn('having a funny conversation',prompt)
            for detail in ('drinks','wine','sunset','laughing loudly'):
                self.assertNotIn(detail,prompt)

    def test_swap_switches_pass_ownership(self):
        first,second=build_sequential_image_prompts('Both sitting at a bar topless','Zara','Molly',adult_mode=True,character_metadata=self.metadata)
        self.assertNotIn('Molly',first)
        self.assertIn('Zara has a bare upper body',first)
        self.assertIn("Preserve Zara exactly as established",second)
        self.assertIn('Molly has a bare upper body',second)
