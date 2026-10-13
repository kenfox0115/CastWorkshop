import unittest
from worker.media_fidelity import fallback_scene, missing_requirements, nsfw_requirements


class MediaFidelityTests(unittest.TestCase):
    def test_oral_action_allows_descriptive_subject_between_verb_and_anatomy(self):
        scene = "The reference character sucks the off-screen adult partner cock."
        self.assertEqual(nsfw_requirements(scene), ["oral_sex"])

    def test_oral_action_recognizes_anatomy_in_mouth_wording(self):
        self.assertIn("oral_sex", nsfw_requirements("Get my cock in your mouth"))

    def test_oral_fallback_is_literal_and_compositionally_bounded(self):
        scene = fallback_scene(["oral_sex", "clothing_removal"])
        self.assertIn("performs the requested oral action", scene)
        self.assertIn("partner's penis", scene)
        self.assertIn("face, torso, and limbs remain outside", scene)
        self.assertIn("garment is visibly pulled down or removed", scene)

    def test_genital_touch_does_not_require_front_facing_visibility(self):
        self.assertNotIn("female_genitals", nsfw_requirements("She rubs her pussy against the floor"))

    def test_inflected_exposure_requires_bare_breasts(self):
        self.assertIn("bare_breasts", nsfw_requirements("She is exposing her breasts"))
        self.assertEqual(missing_requirements(
            "She is exposing her breasts", "She poses in a strapless gown"
        ), ["bare_breasts"])

    def test_masturbation_requires_visible_contact_and_clothing_access(self):
        source = "She digs two fingers into her wet pussy"
        self.assertIn("masturbation", nsfw_requirements(source))
        self.assertEqual(missing_requirements(
            source, "She wears a gown and the prompt merely says she masturbates"
        ), ["masturbation"])
        self.assertEqual(missing_requirements(
            source, "Her gown is lifted and her fingers visibly rub her vulva"
        ), [])

    def test_breast_squeeze_cannot_be_softened_to_hands_near_chest(self):
        source = "She squeezes her breast"
        self.assertIn("breast_squeeze", nsfw_requirements(source))
        self.assertEqual(missing_requirements(
            source, "Her hand rests near her chest"
        ), ["breast_squeeze"])
        self.assertEqual(missing_requirements(
            source, "Her left hand visibly squeezes her bare breast"
        ), [])

    def test_grab_and_cup_remain_distinct_actions(self):
        self.assertEqual(missing_requirements(
            "She grabs one breast", "She cups one breast from below"
        ), ["breast_grab"])
        self.assertEqual(missing_requirements(
            "She cups one breast", "She grabs one breast"
        ), ["breast_cup"])

    def test_butt_grab_cannot_move_to_hip(self):
        source = "She grabs her buttock"
        self.assertEqual(missing_requirements(
            source, "Her hand rests on her hip"
        ), ["butt_grab"])
        self.assertEqual(missing_requirements(
            source, "Her right hand visibly grabs her right buttock"
        ), [])

    def test_exposure_request_rejects_clothed_substitution(self):
        request = "Show me those tits"
        self.assertIn("bare_breasts", nsfw_requirements(request))
        self.assertEqual(missing_requirements(request, "wearing a tube top"), ["bare_breasts"])
        self.assertEqual(missing_requirements(request, "her bare breasts and nipples are visible"), [])

    def test_see_those_tits_requires_visible_bare_breasts(self):
        request = "I need to see those tits"
        self.assertIn("bare_breasts", nsfw_requirements(request))
        self.assertEqual(missing_requirements(request, "her top is partially unfastened"),
                         ["bare_breasts"])

    def test_under_skirt_request_cannot_be_reduced_to_a_hint(self):
        request = "Show me what's up that skirt"
        self.assertIn("under_skirt_reveal", nsfw_requirements(request))
        self.assertEqual(
            missing_requirements(request, "her skirt is partially lifted with a hint of skin"),
            ["under_skirt_reveal"],
        )
        self.assertEqual(
            missing_requirements(
                request,
                "Her skirt is lifted high and her bare vulva and pubic hair are plainly visible",
            ),
            [],
        )

    def test_clothing_removal_must_survive(self):
        request = "Pull that leotard down for me"
        self.assertIn("clothing_removal", missing_requirements(request, "wearing skimpy spandex"))
        self.assertEqual(missing_requirements(request, "she pulls the leotard down"), [])

    def test_completed_removal_rejects_action_that_only_begins(self):
        request = "Take that lingerie off"
        self.assertEqual(
            missing_requirements(
                request, "she begins peeling back her lingerie", True
            ),
            ["clothing_removal_complete"],
        )
        self.assertEqual(
            missing_requirements(
                request, "her lingerie is removed and her bare chest is visible", True
            ),
            [],
        )

    def test_neutral_request_adds_no_requirements(self):
        self.assertEqual(nsfw_requirements("Smile for me in the garden"), [])
        self.assertEqual(nsfw_requirements("Cover your breasts with the towel"), [])
        self.assertEqual(nsfw_requirements("I like your breasts"), [])

    def test_fallback_is_single_character_and_concrete(self):
        scene = fallback_scene(["bare_breasts", "clothing_removal"])
        self.assertIn("adult reference character is alone", scene)
        self.assertIn("fully bare", scene)
        self.assertIn("pulled down or removed", scene)
