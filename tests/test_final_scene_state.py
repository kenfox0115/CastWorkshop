"""Final-state prompts must agree with the requested wardrobe and participants."""
import unittest

from worker.image_prompts import build_image_prompt, build_sequential_image_prompts


class FinalSceneStateTests(unittest.TestCase):
    identities = ((1, 'Ava'), (2, 'Bella'))

    def prompt(self, scene, request=None, adult_mode=True):
        return build_image_prompt(scene, self.identities, latest_request=request,
                                  adult_mode=adult_mode)

    def test_topless_from_clothed_references(self):
        prompt = self.prompt('Ava is wearing a blue dress. Bella is wearing a red blouse.',
                             'both are exposing their bare breasts')
        self.assertIn('Ava is topless', prompt)
        self.assertIn('Bella is topless', prompt)
        self.assertNotIn('blue dress', prompt)
        self.assertNotIn('red blouse', prompt)
        self.assertIn('Do not preserve conflicting upper-body clothing', prompt)

    def test_full_nudity_from_clothed_references(self):
        prompt = self.prompt('Ava is wearing lingerie. Bella is wearing a suit.',
                             'both fully nude')
        self.assertIn('Ava is fully nude', prompt)
        self.assertIn('Bella is fully nude', prompt)
        self.assertNotIn('wearing lingerie', prompt)
        self.assertNotIn('wearing a suit', prompt)

    def test_replacement_clothing_for_both_characters(self):
        prompt = self.prompt('Ava is wearing lingerie. Bella is wearing a suit.',
                             'both change into red evening gowns')
        self.assertIn('Ava is wearing a red evening gown', prompt)
        self.assertIn('Bella is wearing a red evening gown', prompt)
        self.assertNotIn('lingerie', prompt)
        self.assertNotIn('wearing a suit', prompt)

    def test_no_wardrobe_change(self):
        prompt = self.prompt('Ava is wearing a blue dress. Bella is wearing a red blouse.',
                             'stand beside each other')
        self.assertIn('Ava is wearing a blue dress', prompt)
        self.assertIn('Bella is wearing a red blouse', prompt)
        self.assertNotIn('Do not preserve conflicting upper-body clothing', prompt)

    def test_different_replacement_clothing_for_each_character(self):
        prompt = self.prompt('Ava is wearing lingerie. Bella is wearing a suit.',
                             'Ava changes into a blue dress and Bella changes into a red blouse')
        self.assertIn('Ava is wearing a blue dress', prompt)
        self.assertIn('Bella is wearing a red blouse', prompt)
        self.assertNotIn('lingerie', prompt)
        self.assertNotIn('wearing a suit', prompt)

    def test_fragments_become_final_state_sentences(self):
        prompt = self.prompt('both are exposing their bare breasts')
        self.assertIn('Requested edit: Ava and Bella are topless.', prompt)
        self.assertNotIn('exposing', prompt)
        prompt = self.prompt('standing beside each other at the bar')
        self.assertIn('Ava and Bella are standing beside each other at the bar.', prompt)
        for invented in ('smiling', 'cinematic', 'sunset'):
            self.assertNotIn(invented, prompt)

    def test_adult_mode_explicitly_establishes_adulthood(self):
        prompt = self.prompt('stand beside each other')
        self.assertIn('exactly two adult characters', prompt)
        self.assertIn('Keep their identities distinct. Do not blend or duplicate the characters.', prompt)
        self.assertIn('The character is an adult.', build_image_prompt('sit at the bar', adult_mode=True))
        self.assertNotIn('adult', self.prompt('stand beside each other', adult_mode=False))

    def test_individual_change_preserves_other_persons_clothing(self):
        prompt = self.prompt('Ava is wearing a blue dress. Bella is wearing a red blouse.',
                             'Ava is topless')
        self.assertIn('Ava is topless', prompt)
        self.assertIn('Bella is wearing a red blouse', prompt)

    def test_negative_wardrobe_request_does_not_remove_clothing(self):
        for request in ('do not expose their bare breasts', "don't remove their tops", 'do not change into red dresses'):
            with self.subTest(request=request):
                prompt = self.prompt('Ava is wearing a blue dress. Bella is wearing a red blouse.', request)
                self.assertIn('wearing a blue dress', prompt)
                self.assertIn('wearing a red blouse', prompt)
                self.assertNotIn('topless', prompt)

    def test_distinct_wardrobes_in_same_sentence(self):
        prompt = self.prompt('Ava is wearing a dress and Bella is fully nude')
        self.assertIn('Ava is wearing a dress. Bella is fully nude.', prompt)

    def test_first_sequential_pass_preserves_different_final_wardrobes(self):
        first, second = build_sequential_image_prompts(
            'Ava is wearing a dress and Bella is fully nude', 'Ava', 'Bella', adult_mode=True)
        self.assertIn('Ava is wearing a dress.', first)
        self.assertNotIn('Bella', first)
        self.assertNotIn('fully nude', first)
        self.assertIn('Bella is fully nude.', second)
        self.assertNotIn('Ava is wearing a dress', second)
        self.assertNotIn('Ava is fully nude', second)

    def test_sequential_final_pass_keeps_final_wardrobe(self):
        first, second = build_sequential_image_prompts(
            'both are exposing their bare breasts', 'Ava', 'Bella', adult_mode=True)
        self.assertIn('Show exactly one adult character: Ava', first)
        self.assertIn('Bella is topless.', second)
        self.assertIn('exactly two adult characters', second)
        self.assertIn('Do not preserve conflicting upper-body clothing', second)


if __name__ == '__main__':
    unittest.main()
