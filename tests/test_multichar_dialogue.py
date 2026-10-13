"""Offline contracts for exact two-character H3 dialogue."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "worker"))
os.environ.setdefault("VPS_URL", "http://127.0.0.1:1")
from worker import worker as video_worker


class MultiCharacterDialogueTests(unittest.TestCase):
    def test_scene_primary_image_instruction_uses_identity_pictures_two_and_three(self):
        plan = {"visual_scene": "Avery and Blair stand together", "turns": []}
        body = video_worker.build_multichar_image_instruction(
            "Avery", "adult Avery", "Blair", "adult Blair", plan,
            char_a_picture=2, char_b_picture=3,
        )
        self.assertIn("image 2 for Avery's facial identity", body)
        self.assertIn("image 3 for Blair's facial identity", body)
        self.assertNotIn("image 1 for Avery's facial identity", body)

    def test_silent_plan_contains_no_audio_or_dialogue_scaffolding(self):
        scene="Avery and Blair walk toward the window"
        plan=video_worker._fallback_multichar_scene_plan(
            scene,"Avery","Blair",{"a":None,"b":None},[],authoritative_turns=[])
        body=video_worker.build_multichar_h3_video_prompt(
            "Avery","adult","Blair","adult",scene,scene,dialogue_turns=[],scene_plan=plan)
        self.assertNotIn("<Audio",body)
        self.assertNotIn("first authored dialogue line",body)
        self.assertNotIn("Perform each authored turn",body)
        self.assertNotIn("<d>[English]",body)
        self.assertIn("silent scene",body)

    def test_job_1504_omits_biographies_and_empty_speech_clauses(self):
        scene = (
            'They are walking down a quiet street together in the rain, both holding umbrellas. '
            'Jamie says "I got a lot out of that class today" and Lina replies "I\'m so glad"'
        )
        lines = {"a": "I got a lot out of that class today", "b": "I'm so glad"}
        with patch.object(video_worker, "call_ollama") as call:
            body = video_worker.build_multichar_h3_video_prompt(
                "Jamie", "Very short hair. 80s aerobics vibes.",
                "Lina", "A curvy blonde woman, writer vibes", scene, scene,
                spoken_line_a=lines["a"], spoken_line_b=lines["b"], dialogue_order=["a", "b"],
            )
        call.assert_not_called()
        self.assertNotIn("aerobics", body)
        self.assertNotIn("writer vibes", body)
        self.assertNotIn("Very short hair", body)
        self.assertNotIn("Jamie says and Lina replies", body)
        self.assertIn("both holding umbrellas", body)
        self.assertIn("<Subject 1> is Jamie, whose visual identity comes from <Picture 1>", body)
        self.assertIn("<Subject 2> is Lina, whose visual identity comes from <Picture 2>", body)
        self.assertTrue(video_worker._multichar_dialogue_is_exact(body, lines, ["a", "b"]))
        for line in lines.values():
            self.assertEqual(body.count(line), 1)

    def test_visual_cleanup_preserves_action_after_dialogue(self):
        scene = 'Jamie says softly “Hello” and opens the door. Lina responds “Thanks” and walks inside.'
        visual = video_worker._multichar_visual_scene(scene, "Jamie", "Lina")
        self.assertIn("Jamie opens the door", visual)
        self.assertIn("Lina walks inside", visual)
        self.assertNotIn("says", visual)
        self.assertNotIn("responds", visual)
        self.assertNotIn("Hello", visual)
        self.assertEqual(video_worker._multichar_visual_scene(
            "Jamie and Lina walk together.", "Jamie", "Lina"), "Jamie and Lina walk together")

    def test_job_1501_dialogue_is_bound_to_each_character_in_source_order(self):
        scene = (
            'He is sitting in a diner where Darlene is his waitress. She says to him '
            '"good to see you, Hun" to which her replies "good to see you too" as he sits down.'
        )
        line_a, line_b, order = video_worker.extract_multichar_spoken_lines(
            scene, "Alec", "handsome man", "Darlene", "blonde woman waitress",
        )
        self.assertEqual(line_a, "good to see you too")
        self.assertEqual(line_b, "good to see you, Hun")
        self.assertEqual(order, ["b", "a"])

    def test_dialogue_contract_rejects_extra_middle_speech(self):
        lines = {"a": "good to see you too", "b": "good to see you, Hun"}
        valid = (
            "Darlene through <Audio 1>: <d>[English] good to see you, Hun</d>. "
            "Alec through <Audio 2>: <d>[English] good to see you too</d>."
        )
        extra = valid.replace(
            "Alec through",
            "Darlene through <Audio 1>: <d>[English] uh la la</d>. Alec through",
        )
        self.assertTrue(video_worker._multichar_dialogue_is_exact(valid, lines, ["b", "a"]))
        self.assertFalse(video_worker._multichar_dialogue_is_exact(extra, lines, ["b", "a"]))

    def test_deterministic_fallback_has_only_exact_bound_lines(self):
        lines = {"a": "good to see you too", "b": "good to see you, Hun"}
        body = video_worker._deterministic_multichar_h3_prompt(
            "Alec", "handsome man", "Darlene", "woman waitress", "A diner greeting",
            lines, ["b", "a"],
        )
        self.assertTrue(video_worker._multichar_dialogue_is_exact(body, lines, ["b", "a"]))
        self.assertEqual(body.count("<d>"), 2)
        self.assertNotIn('"good to see', body)
        self.assertIn("no human speech or vocalization outside the authored dialogue", body)

    def test_scene_plan_preserves_delivery_action_and_exact_dialogue(self):
        scene = (
            'They sit with coffee. Juno says gratefully “thanks for having me over” '
            'and Alec replies "you are welcome" and winks at her.'
        )
        lines = {"a": "you are welcome", "b": "thanks for having me over"}
        planned = {
            "visual_scene": "Juno and Alec sit together with mugs of coffee",
            "camera": "A stable medium two-shot holds the existing composition",
            "ambience": "Soft room tone",
            "turns": [
                {"speaker": "b", "line": lines["b"], "delivery": "gratefully",
                 "action_before": "", "action_after": ""},
                {"speaker": "a", "line": lines["a"],
                 "delivery": "natural conversational delivery",
                 "action_before": "", "action_after": "Alec winks at Juno"},
            ],
        }
        with patch.object(video_worker, "call_ollama", return_value=__import__("json").dumps(planned)):
            plan = video_worker.build_multichar_scene_plan(
                "Alec", "Juno", scene, lines, ["b", "a"], model="test", keep_alive="2m",
            )
        image_prompt = video_worker.build_multichar_image_instruction(
            "Alec", "man", "Juno", "woman", plan,
        )
        body = video_worker.build_multichar_h3_video_prompt(
            "Alec", "man", "Juno", "woman", scene, scene,
            spoken_line_a=lines["a"], spoken_line_b=lines["b"],
            dialogue_order=["b", "a"], scene_plan=plan,
        )
        self.assertNotIn(lines["a"], image_prompt)
        self.assertNotIn(lines["b"], image_prompt)
        # A post-dialogue gesture belongs to the video timeline, not its
        # opening-frame image. The video must still perform it once.
        self.assertNotIn("Alec winks at Juno", image_prompt)
        self.assertIn("speaks gratefully", body)
        self.assertIn("Alec winks at Juno", body)
        self.assertIn("subject_definitions:", body)
        self.assertIn("detailed_description:", body)
        self.assertIn("non_diegetic_music: N/A", body)
        self.assertIn("<Subject 2> (S1)", body)
        self.assertIn("<Subject 1> (S2)", body)
        self.assertTrue(video_worker._multichar_dialogue_is_exact(body, lines, ["b", "a"]))

    def test_multichar_image_prompt_prevents_identity_and_action_collapse(self):
        plan = {
            "visual_scene": (
                "Bodie and Marnie sit on a balcony while Marnie opens Bodie's top"
            ),
            "turns": [],
        }
        prompt = video_worker.build_multichar_image_instruction(
            "Bodie", "blonde woman with short spiky hair",
            "Marnie", "Filipina woman with long black curly hair",
            plan,
        )
        self.assertIn("image 1 for Bodie's facial identity", prompt)
        self.assertIn("image 2 for Marnie's facial identity", prompt)
        self.assertIn("exactly two women", prompt)
        self.assertIn("Keep their identities distinct. Do not blend or duplicate the characters.", prompt)
        self.assertIn(plan["visual_scene"], prompt)
        self.assertNotIn("blonde woman", prompt)
        self.assertNotIn("<Picture", prompt)
        self.assertLess(len(prompt.split()), 110)

    def test_invalid_scene_plan_cannot_change_dialogue(self):
        lines = {"a": "hello", "b": "hi"}
        invalid = {
            "visual_scene": "A diner",
            "camera": "static",
            "ambience": "room tone",
            "turns": [
                {"speaker": "a", "line": "invented", "delivery": "happy",
                 "action_before": "", "action_after": ""},
                {"speaker": "b", "line": "hi", "delivery": "happy",
                 "action_before": "", "action_after": ""},
            ],
        }
        with patch.object(video_worker, "call_ollama", return_value=__import__("json").dumps(invalid)):
            plan = video_worker.build_multichar_scene_plan(
                "Alec", "Darlene", 'Alec says "hello" and Darlene says "hi"',
                lines, ["a", "b"],
            )
        self.assertEqual([turn["line"] for turn in plan["turns"]], ["hello", "hi"])

    def test_mixed_quotes_and_post_dialogue_wink_are_cleaned(self):
        scene = (
            'Juno says gratefully “thanks for having me” and Alec replies '
            '"you are welcome” and winks at her.'
        )
        visual = video_worker._multichar_visual_scene(scene, "Alec", "Juno")
        self.assertNotIn("thanks", visual)
        self.assertNotIn("welcome", visual)
        self.assertNotIn("and and", visual)
        self.assertIn("Alec winks at her", visual)

    def test_exact_dialogue_bypasses_creative_rewrite(self):
        with patch.object(video_worker, "call_ollama") as call:
            body = video_worker.build_multichar_h3_video_prompt(
                "Alec", "man", "Darlene", "woman", "diner", "greeting",
                spoken_line_a="hi", spoken_line_b="hello", dialogue_order=["b", "a"],
            )
        call.assert_not_called()
        self.assertTrue(video_worker._multichar_dialogue_is_exact(
            body, {"a": "hi", "b": "hello"}, ["b", "a"],
        ))
        self.assertIn("no human speech or vocalization outside the authored dialogue", body)

    def test_duplicate_named_reply_is_resolved_as_alternating_dialogue(self):
        scene = (
            'Walking through a subway Mary says "I am not talking to her for you!" '
            'Mary replies "oh, fine" in a resigned tone.'
        )
        line_a, line_b, order = video_worker.extract_multichar_spoken_lines(
            scene, "Kimmy", "woman in her twenties", "Mary", "woman in her forties",
        )
        self.assertEqual(line_a, "I am not talking to her for you!")
        self.assertEqual(line_b, "oh, fine")
        self.assertEqual(order, ["a", "b"])

    def test_duration_tracks_dialogue_instead_of_always_using_ten_seconds(self):
        self.assertEqual(video_worker.multichar_dialogue_duration("hello", "fine"), 5.0)
        self.assertEqual(video_worker.multichar_dialogue_duration(
            "I am not talking to her for you", "oh fine"), 5.0)

    def test_ordered_turns_preserve_a_b_a_dialogue(self):
        scene = (
            'Alec says "First line." Darlene replies "Second line." '
            'Alec says "Third line."'
        )
        turns = video_worker.extract_multichar_dialogue_turns(
            scene, "Alec", "man", "Darlene", "woman",
        )
        self.assertEqual([turn["speaker"] for turn in turns], ["a", "b", "a"])
        lines = {"a": turns[0]["line"], "b": turns[1]["line"]}
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Alec", "Darlene", lines, ["a", "b", "a"],
            authoritative_turns=video_worker._multichar_performance_hints(
                scene, "Alec", "Darlene", turns,
            ),
        )
        body = video_worker.build_multichar_h3_video_prompt(
            "Alec", "man", "Darlene", "woman", scene, scene,
            dialogue_turns=turns, scene_plan=plan,
        )
        self.assertTrue(video_worker._multichar_dialogue_turns_are_exact(body, turns))
        self.assertEqual(body.count("First line."), 1)
        self.assertEqual(body.count("Second line."), 1)
        self.assertEqual(body.count("Third line."), 1)

    def test_duration_and_camera_include_performance_requirements(self):
        turns = [
            {"speaker": "a", "line": "Thank you for coming over"},
            {"speaker": "b", "line": "I am glad to be here"},
        ]
        plain = {"turns": [
            {"delivery": "natural conversational delivery", "action_before": "", "action_after": ""},
            {"delivery": "natural conversational delivery", "action_before": "", "action_after": ""},
        ]}
        performed = {"turns": [
            {"delivery": "gratefully", "action_before": "", "action_after": "Alec waves"},
            {"delivery": "softly", "action_before": "", "action_after": ""},
        ]}
        self.assertGreater(
            video_worker.multichar_video_duration(turns, performed),
            video_worker.multichar_video_duration(turns, plain),
        )
        self.assertIn("tracking shot", video_worker.multichar_camera_profile("They walk together"))
        self.assertIn("medium-wide", video_worker.multichar_camera_profile("She exits the diner"))
        self.assertIn("two-shot", video_worker.multichar_camera_profile("They are sitting together"))

    def test_recipient_before_then_says_does_not_steal_dialogue(self):
        scene = (
            'They dance together at a festival. Lefty leans and kisses Anna deeply '
            'on the mouth then says “baby, let us leave!” in an enthusiastic and naughty voice.'
        )
        turns = video_worker.extract_multichar_dialogue_turns(
            scene, "Anna", "adult woman", "Lefty", "adult woman",
        )
        self.assertEqual(turns, [{"speaker": "b", "line": "baby, let us leave!"}])
        hints = video_worker._multichar_performance_hints(
            scene, "Anna", "Lefty", turns,
        )
        self.assertEqual(hints[0]["speaker"], "b")
        self.assertIn("Lefty leans and kisses Anna", hints[0]["action_before"])
        self.assertEqual(hints[0]["delivery"], "enthusiastic and naughty")
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Anna", "Lefty", {"a": None, "b": turns[0]["line"]}, ["b"],
            authoritative_turns=hints,
        )
        body = video_worker._deterministic_multichar_h3_prompt(
            "Anna", "adult woman", "Lefty", "adult woman", scene,
            {"a": None, "b": turns[0]["line"]}, ["b"],
            dialogue_turns=turns, scene_plan=plan,
        )
        self.assertIn("Both subjects remain silent during this action. After it is complete", body)
        self.assertIn("<Subject 2> (S1) speaks", body)
        self.assertNotIn("<Subject 2> (S1), Lefty", body)
        self.assertIn("enthusiastic and naughty", body)
        self.assertNotIn("dialogue begins promptly", body)
        self.assertIn("recorded words are excluded", body)
        self.assertTrue(video_worker._multichar_dialogue_turns_are_exact(body, turns))
        self.assertGreaterEqual(video_worker.multichar_video_duration(turns, plan), 8.0)



    def test_repairs_replacement_quotes_and_one_letter_name_typo(self):
        scene = (
            "They dance at a house party. Lyla starts undressing and then says "
            "\ufffdwant to get naughty with me, babe.\ufffd Then Jemma nods."
        )
        turns = video_worker.extract_multichar_dialogue_turns(
            scene, "Lylah", "Korean woman", "Jemma", "white woman",
        )
        self.assertEqual(turns, [
            {"speaker": "a", "line": "want to get naughty with me, babe."}
        ])
        normalized = video_worker.normalize_multichar_dialogue_quotes(scene)
        self.assertIn('"want to get naughty with me, babe."', normalized)
        self.assertNotIn("\ufffd", normalized)


    def test_rejects_planner_audio_cues_outside_exact_dialogue(self):
        turns = [{
            "speaker": "a",
            "line": "I want you to get naughty with me, babe.",
            "delivery": "drunkenly",
            "action_before": "",
            "action_after": "",
        }]
        plan = {
            "visual_scene": "Lylah looks at Jemma and speaks drunkenly.",
            "camera": "A stable medium two-shot.",
            "ambience": "soft laughter and murmurs from guests",
            "turns": turns,
        }
        self.assertFalse(video_worker._validate_multichar_scene_plan(plan, turns))

    def test_first_authored_word_is_first_human_voice(self):
        lines = {"a": "I want you to get naughty with me, babe.", "b": None}
        turns = [{"speaker": "a", "line": lines["a"]}]
        body = video_worker._deterministic_multichar_h3_prompt(
            "Lylah", "Korean woman", "Jemma", "white woman",
            'They dance. Lylah says "I want you to get naughty with me, babe."',
            lines, ["a"], dialogue_turns=turns,
        )
        self.assertIn("no human speech or vocalization outside the authored dialogue", body)
        self.assertTrue(video_worker._multichar_dialogue_turns_are_exact(body, turns))

    def test_background_music_is_instrumental_ambience_not_visual_blocking(self):
        scene = ('Sitting at the bar next to each other. Andrea says "These guys are really good!" '
                 'We hear the band playing jazz music in the background.')
        turns = [{"speaker": "a", "line": "These guys are really good!"}]
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Andrea", "Anthony", {"a": turns[0]["line"], "b": None}, ["a"],
            video_worker._multichar_performance_hints(scene, "Andrea", "Anthony", turns),
        )
        self.assertNotIn("hear", plan["visual_scene"].lower())
        self.assertNotIn("music", plan["visual_scene"].lower())
        body = video_worker._deterministic_multichar_h3_prompt(
            "Andrea", "adult", "Anthony", "adult", scene,
            {"a": turns[0]["line"], "b": None}, ["a"], turns, plan,
        )
        self.assertNotIn("jazz", plan["ambience"].lower())
        self.assertIn("diegetic music remains audible", body.lower())
        self.assertIn("instrumental, without singing or spoken words", body.lower())

    def test_one_speaker_prompt_declares_only_that_voice_reference(self):
        body = video_worker._deterministic_multichar_h3_prompt(
            "Andrea", "adult", "Anthony", "adult", 'Andrea says "Hello."',
            {"a": "Hello.", "b": None}, ["a"],
        )
        self.assertIn("<Audio 1> is the voice-timbre reference for <Subject 1>", body)
        self.assertNotIn("<Audio 2>", body)


    def test_second_only_speaker_has_unique_stable_identity(self):
        body = video_worker._deterministic_multichar_h3_prompt(
            "Alex", "adult", "Robin", "adult", 'Robin says "Ready."',
            {"a": None, "b": "Ready."}, ["b"],
        )
        self.assertIn("<Subject 2> (S1)", body)
        self.assertNotIn("<Subject 2> (S2)", body)
        self.assertIn("<Audio 1> is the voice-timbre reference for <Subject 2> (S1)", body)
        self.assertNotIn("<Audio 2>", body)

    def test_dialogue_timeline_fills_before_and_after_without_extra_speech(self):
        body = video_worker._deterministic_multichar_h3_prompt(
            "Alex", "adult", "Robin", "adult", 'Alex says "Welcome."',
            {"a": "Welcome.", "b": None}, ["a"],
        )
        self.assertIn("After the final line", body)
        self.assertIn("without further speech", body)

    def test_fallback_rejects_unresolved_vocalization(self):
        with self.assertRaisesRegex(ValueError, "unresolved speech"):
            video_worker._fallback_multichar_scene_plan(
                'Alex is laughing. Robin says "Ready."', "Alex", "Robin",
                {"a": None, "b": "Ready."}, ["b"],
            )

    def test_shared_attribution_and_post_dialogue_sequence(self):
        scene = 'Robin purrs: "Ready." She takes Alex by the hand and leads Alex outside.'
        turns = video_worker.extract_multichar_dialogue_turns(
            scene, "Alex", "adult", "Robin", "adult",
        )
        self.assertEqual(turns, [{"speaker": "b", "line": "Ready."}])
        hints = video_worker._multichar_performance_hints(scene, "Alex", "Robin", turns)
        self.assertIn("Robin takes Alex", hints[0]["action_after"])
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Alex", "Robin", {"a": None, "b": "Ready."}, ["b"], hints,
        )
        self.assertNotIn("takes Alex", plan["visual_scene"])
        self.assertNotIn("purrs", plan["visual_scene"])
        body = video_worker._deterministic_multichar_h3_prompt(
            "Alex", "adult", "Robin", "adult", scene,
            {"a": None, "b": "Ready."}, ["b"], turns, plan,
        )
        self.assertLess(body.index("Ready.</d>"), body.index("Robin takes Alex"))

    def test_compiler_rejects_modified_supplied_plan(self):
        scene = 'Robin says "Ready."'
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Alex", "Robin", {"a": None, "b": "Ready."}, ["b"],
        )
        plan["turns"][0]["line"] = "Changed."
        with self.assertRaises(ValueError):
            video_worker._deterministic_multichar_h3_prompt(
                "Alex", "adult", "Robin", "adult", scene,
                {"a": None, "b": "Ready."}, ["b"], scene_plan=plan,
            )

    def test_no_dialogue_legacy_call_does_not_invent_speech(self):
        with patch.object(video_worker, "call_ollama") as model:
            body = video_worker.build_multichar_h3_video_prompt(
                "Alex", "adult", "Robin", "adult",
                "Both stand near a door wearing pants.", "",
            )
        model.assert_not_called()
        self.assertNotIn("<d>", body)
        self.assertIn("wearing pants", body)

    def test_unattributed_quotes_are_not_assigned_by_position(self):
        turns = video_worker.extract_multichar_dialogue_turns(
            'They pause. "First line." Then someone says nothing. "Second line."',
            "Alex", "adult", "Robin", "adult",
        )
        self.assertEqual(turns, [])

    def test_varied_dry_run_parser_regressions(self):
        quote_first = video_worker.extract_multichar_dialogue_turns(
            '"Wait near the gate," says Alex while Robin checks the road.',
            "Alex", "adult", "Robin", "adult",
        )
        self.assertEqual(quote_first, [{"speaker": "a", "line": "Wait near the gate,"}])

        sign = 'Alex and Robin walk past a neon sign reading "OPEN ALL NIGHT".'
        self.assertEqual(video_worker.extract_multichar_dialogue_turns(
            sign, "Alex", "adult", "Robin", "adult",
        ), [])
        self.assertIn("OPEN ALL NIGHT", video_worker._multichar_visual_scene(
            sign, "Alex", "Robin",
        ))

        for scene, expected_action in (
            ('Robin embraces Alex, then says "Welcome home."', "embraces"),
            ('Alex removes a jacket, then says "I am ready."', "removes"),
        ):
            turns = video_worker.extract_multichar_dialogue_turns(
                scene, "Alex", "adult", "Robin", "adult",
            )
            hints = video_worker._multichar_performance_hints(
                scene, "Alex", "Robin", turns,
            )
            self.assertIn(expected_action, hints[0]["action_before"])

    def test_non_authored_human_sounds_fail_before_prompt_compilation(self):
        for sound in ("moans", "groans", "gasps", "sighs", "laughs"):
            with self.subTest(sound=sound), self.assertRaises(ValueError):
                video_worker._fallback_multichar_scene_plan(
                    f"Alex and Robin stand together while Robin {sound}.",
                    "Alex", "Robin", {"a": None, "b": None}, [],
                )

    def test_attached_gasp_becomes_silent_action_before_exact_dialogue(self):
        scene = (
            "They jump on a trampoline. Lola gasps and says "
            '"oh, that looks delicious"'
        )
        turns = video_worker.extract_multichar_dialogue_turns(
            scene, "Lola", "adult woman", "Lylah", "adult woman",
        )
        self.assertEqual(turns, [
            {"speaker": "a", "line": "oh, that looks delicious"}
        ])
        hints = video_worker._multichar_performance_hints(
            scene, "Lola", "Lylah", turns,
        )
        self.assertEqual(
            hints[0]["action_before"], "Lola takes a quick visible breath"
        )
        plan = video_worker._fallback_multichar_scene_plan(
            scene, "Lola", "Lylah",
            {"a": turns[0]["line"], "b": None}, ["a"],
            authoritative_turns=hints,
        )
        self.assertNotIn("gasp", plan["visual_scene"].lower())
        body = video_worker.build_multichar_h3_video_prompt(
            "Lola", "adult woman", "Lylah", "adult woman", scene, scene,
            spoken_line_a=turns[0]["line"], dialogue_order=["a"],
            dialogue_turns=turns, scene_plan=plan,
        )
        self.assertIn("Lola takes a quick visible breath", body)
        self.assertIn("<d>[English] oh, that looks delicious</d>", body)

if __name__ == "__main__":
    unittest.main()
