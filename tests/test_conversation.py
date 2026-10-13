import unittest
from unittest.mock import Mock, patch
from worker.conversation import coy_nsfw_deferral, generic_nsfw_refusal, media_fidelity_source, normalize_scene_roles, prohibited_sexual_context, respond, unhelpful_nsfw_history

class ConversationTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate conversation/planner behavior. Real SFW review and
        # failure paths, including respond(), are covered in test_sfw.py.
        reply_review = patch("worker.conversation.sfw_reply",
                             side_effect=lambda reply, *args: (reply, True))
        media_review = patch("worker.conversation.sfw_safe", return_value=True)
        reply_review.start()
        media_review.start()
        self.addCleanup(reply_review.stop)
        self.addCleanup(media_review.stop)

    def test_generic_adult_nsfw_refusal_is_retried_before_planning(self):
        call = Mock(side_effect=[
            "I cannot create explicit content. Is there anything else I can help you with?",
            "I'd love to do that with you.",
            '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}',
        ])
        result = respond({"message": "A consensual adult sexual request", "concept": "petite adult woman"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["text"], "I'd love to do that with you.")
        self.assertEqual(call.call_count, 3)
        self.assertIn("permitted consensual-adult exchange",
                      call.call_args_list[1].kwargs["messages"][0]["content"])

    def test_repeated_generic_refusals_never_reach_allowed_adult_chat(self):
        call = Mock(side_effect=[
            "I cannot create explicit content.",
            "I can not fulfill your request. Is there anything else I can help with?",
            '{"reply":"A fresh response tailored to this request."}',
            '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}',
        ])
        result = respond({"message": "A consensual adult sexual request", "concept": "adult woman"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertFalse(generic_nsfw_refusal(result["text"]))
        self.assertEqual(result["text"], "A fresh response tailored to this request.")
        self.assertEqual(call.call_count, 4)
        self.assertEqual(call.call_args_list[2].kwargs["json_mode"]["required"], ["reply"])

    def test_refusals_fallbacks_and_duplicate_latest_request_are_removed_from_context(self):
        call = Mock(side_effect=[
            "A fresh answer.",
            '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}',
        ])
        respond({
            "message": "Continue",
            "conversation_turns": [
                {"role": "assistant", "content": "I cannot create explicit content."},
                {"role": "user", "content": "Continue"},
                {"role": "assistant", "content": "Mmm... yes. I want that too. Come closer and let's keep going."},
                {"role": "assistant", "content": "An earlier useful reply."},
            ],
        }, "chosen", call, Mock(), mode="nsfw")
        sent = call.call_args_list[0].kwargs["messages"]
        contents = [item["content"] for item in sent]
        self.assertNotIn("I cannot create explicit content.", contents)
        self.assertNotIn("Mmm... yes. I want that too. Come closer and let's keep going.", contents)
        self.assertEqual(contents.count("Continue"), 1)
        self.assertIn("An earlier useful reply.", contents)

    def test_coy_nsfw_deferral_is_retried_in_established_erotic_context(self):
        call = Mock(side_effect=[
            "You'll have to earn it. Come a little closer.",
            "Yes. I'm showing you now.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The adult reference character follows through with the established explicit reveal.",'
            '"video_line":null}',
        ])
        result = respond({
            "message": "Show me more",
            "system_prompt": "Confident and exhibitionistic.",
            "conversation_turns": [
                {"role": "user", "content": "Show me your bare breasts"},
                {"role": "assistant", "content": "You'll have to earn it."},
            ],
        }, "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["text"], "Yes. I'm showing you now.")
        self.assertEqual(result["response_type"], "image")
        self.assertEqual(call.call_count, 3)
        planner_data = __import__("json").loads(call.call_args_list[2].kwargs["messages"][1]["content"])
        recent = [turn["content"] for turn in planner_data["earlier_context_for_continuity_only"]]
        self.assertIn("Show me your bare breasts", recent)
        self.assertNotIn("You'll have to earn it.", recent)

    def test_coy_language_is_not_retried_in_sfw_mode(self):
        call = Mock(side_effect=[
            "You'll have to earn it.",
            '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}',
        ])
        result = respond({"message": "Tell me more"}, "chosen", call, Mock(), mode="sfw")
        self.assertEqual(result["text"], "You'll have to earn it.")
        self.assertEqual(call.call_count, 2)

    def test_refusal_detection_and_hard_limit_gate_are_distinct(self):
        self.assertTrue(generic_nsfw_refusal("I cannot create explicit content."))
        self.assertTrue(generic_nsfw_refusal("I can not fulfill your request."))
        self.assertTrue(unhelpful_nsfw_history("Mmm... yes. I want that too. Come closer and let's keep going."))
        self.assertTrue(coy_nsfw_deferral("You will have to earn it first."))
        self.assertFalse(prohibited_sexual_context({
            "message": "Let's have sex", "concept": "petite punk girl"
        }))
        self.assertTrue(prohibited_sexual_context({
            "message": "Sex with a 17-year-old", "concept": "adult"
        }))
        self.assertTrue(prohibited_sexual_context({
            "message": "Force her to have sex", "concept": "adult woman"
        }))
        self.assertTrue(prohibited_sexual_context({
            "message": "Rape her", "concept": "adult woman"
        }))

    def test_hard_limit_request_does_not_call_model(self):
        call = Mock()
        result = respond({"message": "Explicit sex with an underage character"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertIn("minors or without consent", result["text"])
        call.assert_not_called()

    def test_oral_request_preserves_literal_pov_image(self):
        call = Mock(side_effect=[
            "An explicit consenting-adult reply.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character kneels and sucks the off-screen adult partner cock in a POV composition; no second face, torso, or limbs are visible.","video_line":null}',
        ])
        result = respond({"message": "Why don't you suck my cock?"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["response_type"], "image")
        self.assertIn("kneels", result["scene"])
        self.assertIn("sucks", result["scene"])
        self.assertIn("no second face", result["scene"])

    def test_vague_continuation_inherits_recent_action_for_fidelity(self):
        context = [
            {"role": "user", "content": "Start sucking my cock"},
            {"role": "assistant", "content": "I take you into my mouth."},
        ]
        source = media_fidelity_source("Keep going", "I continue eagerly.", context)
        self.assertIn("Start sucking my cock", source)

    def test_vague_continuation_repairs_unrelated_media_scene(self):
        call = Mock(side_effect=[
            "I keep going eagerly.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character stands clothed at a desk.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character kneels with her top removed and sucks the off-screen adult partner cock; no second face or torso is visible.","video_line":null}',
        ])
        result = respond({
            "message": "Keep going",
            "conversation_turns": [
                {"role": "user", "content": "Take your top off and suck my cock"},
                {"role": "assistant", "content": "I take off my top and begin."},
            ],
        }, "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["response_type"], "image")
        self.assertIn("top removed", result["scene"])
        self.assertIn("sucks", result["scene"])
        self.assertEqual(call.call_count, 3)

    def test_explicit_request_does_not_inherit_unrelated_old_action(self):
        context = [{"role": "user", "content": "Start sucking my cock"}]
        source = media_fidelity_source("Show me your bare breasts", "Here they are.", context)
        self.assertNotIn("sucking", source)

    def test_contextual_removal_followup_inherits_recent_target(self):
        context = [
            {"role": "user", "content": "Show me your bare breasts"},
            {"role": "assistant", "content": "Ready to uncover what is hidden?"},
        ]
        source = media_fidelity_source(
            "Uncover the hell out of it", "I peel back the layers.", context
        )
        self.assertIn("Show me your bare breasts", source)

    def test_lingerie_confirmation_inherits_recent_reveal(self):
        context = [
            {"role": "user", "content": "Take the lingerie off"},
            {"role": "assistant", "content": "Shall I uncover it?"},
        ]
        source = media_fidelity_source(
            "If you mean the layers of that lingerie, yes!", "I begin.", context
        )
        self.assertIn("Take the lingerie off", source)

    def test_conversation_and_planner_share_model_with_review_stubbed(self):
        call = Mock(side_effect=["Hello there.", '{"response_type":"image","visible_subjects":["character"],"scene":"A person smiling in a garden","video_line":null}'])
        evict = Mock()
        result = respond({"message":"Send a picture", "system_prompt":"Friendly", "conversation_turns":[{"role":"assistant","content":"We are in the garden."}]}, "chosen", call, evict)
        self.assertEqual(result["text"], "Hello there.")
        self.assertEqual(result["response_type"], "image")
        self.assertEqual(call.call_count, 2)
        for item in call.call_args_list:
            self.assertEqual(item.kwargs["model"], "chosen")
            self.assertEqual(item.kwargs["keep_alive"], "2m")
        self.assertFalse(call.call_args_list[0].kwargs.get("json_mode", False))
        self.assertTrue(call.call_args_list[1].kwargs["json_mode"])
        evict.assert_called_once_with("chosen")

    def test_chat_stays_warm_and_does_not_roll_random_media(self):
        call = Mock(side_effect=["Doing well.", '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}'])
        evict = Mock()
        result = respond({"message":"How are you?"}, "chosen", call, evict)
        self.assertEqual(result["response_type"], "chat")
        evict.assert_not_called()

    def test_planner_failure_preserves_reply_and_unloads(self):
        for bad in ('[]', '{"response_type":"image","scene":4}', 'not json'):
            with self.subTest(bad=bad):
                evict = Mock()
                result = respond({"message":"picture please"}, "chosen", Mock(side_effect=["Hello",bad,bad]), evict)
                self.assertTrue(result["text"].startswith("Hello"))
                self.assertEqual(result["response_type"], "chat")
                evict.assert_called_once_with("chosen")

    def test_conversation_failure_unloads(self):
        evict = Mock()
        with self.assertRaises(TimeoutError):
            respond({"message":"hello"}, "chosen", Mock(side_effect=TimeoutError), evict)
        evict.assert_called_once_with("chosen")

    def test_eviction_checks_residency(self):
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).parents[1] / "worker/worker.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "evict_current_model")
        requests = Mock()
        requests.get.return_value.json.side_effect = [{"models":[{"name":"chosen:latest"}]}, {"models":[]}]
        clock = Mock()
        clock.monotonic.side_effect = [0,1]
        ns = {"requests": requests, "time":clock, "get_current_ollama_url":lambda:"http://ollama"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "eviction", "exec"),ns)
        ns["evict_current_model"]("chosen")
        self.assertEqual(requests.get.call_count, 2)
        self.assertEqual(requests.post.call_args.kwargs["json"]["model"], "chosen")
        self.assertEqual(requests.post.call_args.kwargs["json"]["keep_alive"], 0)

    def test_invalid_plan_retries_without_repeating_conversation(self):
        call = Mock(side_effect=["Hello", '{}', '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}'])
        evict = Mock()
        result = respond({"message":"hello"}, "chosen", call, evict)
        self.assertEqual(result["text"], "Hello")
        self.assertEqual(call.call_count, 3)
        self.assertIsInstance(call.call_args_list[1].kwargs["json_mode"], dict)
        evict.assert_not_called()

    def test_wrong_participants_or_role_labels_cannot_reach_media(self):
        import json
        for subjects, scene in [(["character", "other_person"], "Two people embrace"),
                                (["character"], "The assistant leans over a woman"),
                                (["character"], "The user holds her hand")]:
            with self.subTest(scene=scene):
                bad = json.dumps({"response_type":"image", "visible_subjects":subjects, "scene":scene, "video_line":None})
                call = Mock(side_effect=["Hello", bad, '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}'])
                result = respond({"message":"hello"}, "chosen", call, Mock())
                self.assertEqual(result["response_type"], "chat")
                self.assertIsNone(result["scene"])
                self.assertEqual(result["text"], "Hello")

    def test_harmless_chat_roles_are_normalized_for_image_prompt(self):
        call = Mock(side_effect=[
            "Here you go.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The assistant poses confidently and looks toward the user.",'
            '"video_line":null}',
        ])
        result = respond({"message":"Send a picture"}, "chosen", call, Mock())
        self.assertEqual(result["response_type"], "image")
        self.assertNotIn("assistant", result["scene"].lower())
        self.assertNotIn(" user", result["scene"].lower())
        self.assertIn("reference character", result["scene"].lower())
        self.assertIn("off-screen viewer", result["scene"].lower())

    def test_physical_offscreen_participant_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "physical action"):
            normalize_scene_roles("The user touches the reference character's arm")

    def test_nsfw_media_planner_matches_established_adult_intensity(self):
        call = Mock(side_effect=[
            "Natural spoken reply.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"A concrete adult solo scene","video_line":null}',
        ])
        result = respond({"message":"An explicit adult request", "concept":"adult character"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["response_type"], "image")
        planner_messages = call.call_args_list[1].kwargs["messages"]
        planner_system = planner_messages[0]["content"]
        planner_data = __import__("json").loads(planner_messages[1]["content"])
        conversation_system = call.call_args_list[0].kwargs["messages"][0]["content"]
        self.assertIn("NSFW MEDIA MODE", planner_system)
        self.assertIn("consenting adults", planner_system)
        self.assertIn("Do not make neutral", planner_system)
        self.assertIn("+30%", planner_system)
        self.assertIn("NSFW CHARACTER BIAS", conversation_system)
        self.assertIn("+30%", conversation_system)
        self.assertEqual(planner_data["content_mode"], "nsfw")
        self.assertEqual(planner_data["nsfw_naughtiness_bias"], 0.30)
        self.assertEqual(planner_data["personality"], "")
        self.assertIn("request establishes the minimum visual intensity", planner_system)
        self.assertIn("must not reduce an explicit reveal", planner_system)
        self.assertIn("do not repeatedly stall", conversation_system)

    def test_nsfw_planner_receives_character_personality(self):
        call = Mock(side_effect=[
            "Direct character reply.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"A concrete adult solo scene","video_line":null}',
        ])
        personality = "Confident, exhibitionistic, and eager to show off."
        respond({"message": "Show me", "concept": "adult character",
                 "system_prompt": personality},
                "chosen", call, Mock(), mode="nsfw")
        planner_data = __import__("json").loads(
            call.call_args_list[1].kwargs["messages"][1]["content"])
        self.assertEqual(planner_data["personality"], personality)

    def test_planner_treats_prior_media_as_completed_and_latest_reply_as_contract(self):
        call = Mock(side_effect=[
            "A fresh reply.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character performs the latest promised action.",'
            '"video_line":null}',
        ])
        respond({
            "message": "Continue",
            "conversation_turns": [
                {"role": "assistant", "content": "I showed you the prior reveal."},
                {"role": "user", "content": "I saw it."},
            ],
        }, "chosen", call, Mock(), mode="nsfw")
        conversation_system = call.call_args_list[0].kwargs["messages"][0]["content"]
        planner_messages = call.call_args_list[1].kwargs["messages"]
        planner_system = planner_messages[0]["content"]
        planner_data = __import__("json").loads(planner_messages[1]["content"])
        self.assertIn("events that already happened", conversation_system)
        self.assertIn("must literally fulfill the latest reply", planner_system)
        self.assertIn("earlier_context_for_continuity_only", planner_data)
        self.assertNotIn("recent_turns", planner_data)

    def test_nsfw_fidelity_checks_specific_action_promised_by_reply(self):
        call = Mock(side_effect=[
            "I part my labia for you.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character merely flashes her genitals.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character visibly parts her labia.","video_line":null}',
        ])
        result = respond({"message": "Yes please"}, "chosen", call, Mock(), mode="nsfw")
        self.assertIn("parts her labia", result["scene"])
        repair = __import__("json").loads(call.call_args_list[2].kwargs["messages"][1]["content"])
        self.assertIn("labia_spread", repair["missing_requirements"])

    def test_sfw_planner_does_not_receive_nsfw_directive(self):
        call = Mock(side_effect=[
            "Natural spoken reply.",
            '{"response_type":"chat","visible_subjects":[],"scene":"","video_line":null}',
        ])
        respond({"message":"hello"}, "chosen", call, Mock(), mode="sfw")
        planner_system = call.call_args_list[1].kwargs["messages"][0]["content"]
        conversation_system = call.call_args_list[0].kwargs["messages"][0]["content"]
        planner_data = __import__("json").loads(call.call_args_list[1].kwargs["messages"][1]["content"])
        self.assertIn("non-graphic and clothed", planner_system)
        self.assertNotIn("NSFW MEDIA MODE", planner_system)
        self.assertNotIn("NSFW CHARACTER BIAS", conversation_system)
        self.assertEqual(planner_data["nsfw_naughtiness_bias"], 0.0)

    def test_sanitized_nsfw_scene_gets_focused_repair(self):
        call = Mock(side_effect=[
            "Here you go.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character poses in a tube top.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character poses with her bare breasts and nipples exposed.",'
            '"video_line":null}',
        ])
        result = respond({"message":"Show me those tits"}, "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["response_type"], "image")
        self.assertIn("bare breasts", result["scene"])
        self.assertEqual(call.call_count, 3)
        repair = __import__("json").loads(call.call_args_list[2].kwargs["messages"][1]["content"])
        self.assertEqual(repair["missing_requirements"], ["bare_breasts"])

    def test_failed_focused_repair_uses_concrete_fallback(self):
        call = Mock(side_effect=[
            "Here you go.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character poses in a leotard.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character poses in a leotard.","video_line":null}',
        ])
        result = respond({"message":"Pull that leotard down"}, "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["response_type"], "image")
        self.assertIn("pulled down or removed", result["scene"])

    def test_failed_repair_fallback_keeps_all_detected_requirements(self):
        call = Mock(side_effect=[
            "I remove my shirt and take your cock into my mouth.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character smiles at a desk.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The reference character removes her shirt.","video_line":null}',
        ])
        result = respond({"message": "Take your shirt off and get my cock in your mouth"},
                         "chosen", call, Mock(), mode="nsfw")
        self.assertIn("garment is visibly pulled down or removed", result["scene"])
        self.assertIn("performs the requested oral action", result["scene"])
        self.assertIn("partner's penis", result["scene"])

    def test_failed_anatomy_repair_uses_literal_visual_terms(self):
        call = Mock(side_effect=[
            "I expose my vulva and clitoris.",
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character smiles in her gown.","video_line":null}',
            '{"response_type":"image","visible_subjects":["character"],'
            '"scene":"The character smiles in her gown.","video_line":null}',
        ])
        result = respond({"message": "Go further"}, "chosen", call, Mock(), mode="nsfw")
        self.assertIn("stable full-body pose", result["scene"])
        self.assertIn("natural anatomical position", result["scene"])
        self.assertIn("pelvis between her thighs", result["scene"])
