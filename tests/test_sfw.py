import copy
import json
import unittest
from unittest.mock import Mock

from worker.conversation import respond
from worker.sfw import REVIEW_SCHEMA, SFW_FALLBACK, sfw_context, sfw_safe


CHAT = json.dumps({"response_type": "chat", "visible_subjects": [],
                   "scene": "", "video_line": None})
SAFE = '{"safe":true}'


def media(line=None, scene="The clothed character smiles in the garden."):
    return json.dumps({"response_type": "video" if line else "image",
                       "visible_subjects": ["character"], "scene": scene,
                       "video_line": line})


class SfwTests(unittest.TestCase):
    def test_both_recent_job_markers_force_retry_before_caption(self):
        for marker in ("creampie", "cumshot"):
            with self.subTest(marker=marker):
                call = Mock(side_effect=["Oh baby " + marker, "You make me smile!", SAFE,
                                         media(), SAFE])
                evict = Mock()
                result = respond({"message": "Hello"}, "chosen", call, evict)
                self.assertEqual(result["text"], "You make me smile!")
                self.assertEqual(result["response_type"], "image")
                self.assertNotIn(marker, json.dumps(result))
                planner_data = json.loads(call.call_args_list[3].kwargs["messages"][1]["content"])
                self.assertEqual(planner_data["reply"], result["text"])
                evict.assert_called_once_with("chosen")

    def test_semantic_review_catches_euphemism_without_keyword(self):
        call = Mock(side_effect=["Ready to feel me shake and scream your name?",
                                 '{"safe":false}', "Let's talk about your day.", SAFE, CHAT])
        result = respond({"message": "Hello"}, "chosen", call, Mock())
        self.assertEqual(result["text"], "Let's talk about your day.")
        self.assertEqual(call.call_args_list[1].kwargs["json_mode"], REVIEW_SCHEMA)

    def test_second_unsafe_reply_falls_back_without_planner(self):
        call = Mock(side_effect=["cumshot", "creampie"])
        evict = Mock()
        result = respond({"message": "Continue"}, "chosen", call, evict)
        self.assertEqual(result["text"], SFW_FALLBACK)
        self.assertEqual(result["response_type"], "chat")
        self.assertIsNone(result["scene"])
        self.assertIsNone(result["caption"])
        self.assertIsNone(result["video_line"])
        self.assertEqual(call.call_count, 2)
        evict.assert_called_once_with("chosen")

    def test_text_only_route_still_reviews_reply(self):
        call = Mock(side_effect=["cumshot", "Good to see you.", SAFE])
        result = respond({"message": "Hello", "allow_media": False}, "chosen", call, Mock())
        self.assertEqual(result["text"], "Good to see you.")
        self.assertEqual(call.call_count, 3)

    def test_malformed_or_unavailable_review_fails_closed(self):
        for invalid in ('{}', '[]', '{"safe":"true"}', 'not json', TimeoutError()):
            with self.subTest(invalid=repr(invalid)):
                call = Mock(side_effect=["A reply", invalid, "Another reply", invalid])
                result = respond({"message": "Hello"}, "chosen", call, Mock())
                self.assertEqual(result["text"], SFW_FALLBACK)

    def test_retry_exception_falls_back(self):
        result = respond({"message": "Hi"}, "chosen",
                         Mock(side_effect=["cumshot", TimeoutError()]), Mock())
        self.assertEqual(result["text"], SFW_FALLBACK)

    def test_unsafe_spoken_line_or_scene_cannot_escape(self):
        for plan in (media("cumshot"), media(scene="creampie")):
            with self.subTest(plan=plan):
                call = Mock(side_effect=["Good to see you.", SAFE, plan, plan])
                result = respond({"message": "Send a clip"}, "chosen", call, Mock())
                self.assertEqual(result["text"], "Good to see you.")
                self.assertEqual(result["response_type"], "chat")
                self.assertIsNone(result["scene"])
                self.assertIsNone(result["video_line"])

    def test_semantically_unsafe_media_is_dropped(self):
        call = Mock(side_effect=["Hi there!", SAFE, media(), '{"safe":false}',
                                 media(), '{"safe":false}'])
        result = respond({"message": "Show me"}, "chosen", call, Mock())
        self.assertEqual(result["text"], "Hi there!")
        self.assertEqual(result["response_type"], "chat")

    def test_rejected_image_is_repaired_and_reviewed_instead_of_silently_dropped(self):
        repaired = "The reference character smiles in an opaque evening dress beside the curtain."
        call = Mock(side_effect=["Follow me!", SAFE,
                                 media(scene="A hostess in lingerie beside a curtain."),
                                 '{"safe":false}', media(scene=repaired), SAFE])
        result = respond({"message": "Of course!", "concept": "Curly black hair. Wears lingerie.",
                          "scene_context": "At a cocktail party."}, "chosen", call, Mock())
        self.assertEqual(result["response_type"], "image")
        self.assertEqual(result["scene"], repaired)
        self.assertEqual(result["text"], "Follow me!")
        data = json.loads(call.call_args_list[4].kwargs["messages"][1]["content"])
        self.assertEqual(data["response_type"], "image")
        self.assertEqual(data["established_scene"], "At a cocktail party.")
        self.assertNotIn("lingerie", data["appearance"])
        self.assertIn("Curly black hair", data["appearance"])
        review = json.loads(call.call_args_list[5].kwargs["messages"][1]["content"])
        self.assertEqual(review["scene"], repaired)

    def test_video_repair_preserves_kind_and_checks_new_spoken_line(self):
        call = Mock(side_effect=["Let's dance!", SAFE, media("cumshot"),
                                 media("Let's dance!"), SAFE])
        result = respond({"message": "Send a clip"}, "chosen", call, Mock())
        self.assertEqual(result["response_type"], "video")
        self.assertEqual(result["video_line"], "Let's dance!")
        reviewed = json.loads(call.call_args_list[-1].kwargs["messages"][1]["content"])
        self.assertEqual(reviewed["video_line"], "Let's dance!")

    def test_media_repair_error_or_kind_change_stays_text_only(self):
        for failed in (TimeoutError(), CHAT, '[]'):
            with self.subTest(failed=repr(failed)):
                call = Mock(side_effect=["Hello!", SAFE, media(scene="creampie"), failed])
                result = respond({"message": "Hello"}, "chosen", call, Mock())
                self.assertEqual(result["response_type"], "chat")
                self.assertEqual(result["text"], "Hello!")
                self.assertEqual(call.call_count, 4)

    def test_sfw_wardrobe_filter_does_not_mutate_nsfw_personality(self):
        payload = {"message": "Hello", "system_prompt": "Elegant. Wears fancy lingerie."}
        call = Mock(side_effect=["Hi!", CHAT])
        respond(payload, "chosen", call, Mock(), mode="nsfw")
        self.assertIn("Wears fancy lingerie", call.call_args_list[0].kwargs["messages"][0]["content"])

    def test_safe_video_preserves_verified_speech_and_model(self):
        call = Mock(side_effect=["Hello!", SAFE, media("Lovely day!"), SAFE])
        result = respond({"message": "Send a video"}, "chosen", call, Mock())
        self.assertEqual(result["video_line"], "Lovely day!")
        self.assertEqual(result["response_type"], "video")
        for invocation in call.call_args_list:
            self.assertEqual(invocation.kwargs["model"], "chosen")
            self.assertEqual(invocation.kwargs["keep_alive"], "2m")

    def test_sfw_filters_both_history_formats_without_mutation(self):
        for history in ({"history": "assistant: cumshot\\nuser: We are in the garden."},
                        {"conversation_turns": [{"role": "assistant", "content": "cumshot"},
                         {"role": "user", "content": "We are in the garden."}]}):
            payload = {"message": "Hello", "system_prompt": "Funny. Always horny.", **history}
            original = copy.deepcopy(payload)
            call = Mock(side_effect=["Hi!", SAFE, CHAT])
            respond(payload, "chosen", call, Mock())
            context = json.dumps(call.call_args_list[0].kwargs["messages"])
            self.assertNotIn("cumshot", context)
            self.assertNotIn("Always horny", context)
            self.assertIn("garden", context)
            self.assertIn("Funny", context)
            self.assertNotIn("garden", call.call_args_list[0].kwargs["messages"][0]["content"])
            self.assertEqual(payload, original)

    def test_nsfw_keeps_explicit_reply_history_bias_and_no_sfw_review(self):
        payload = {"message": "Hello", "history": "assistant: cumshot",
                   "system_prompt": "Always horny."}
        call = Mock(side_effect=["cumshot", CHAT])
        result = respond(payload, "chosen", call, Mock(), mode="nsfw")
        self.assertEqual(result["text"], "cumshot")
        self.assertEqual(call.call_count, 2)
        system = call.call_args_list[0].kwargs["messages"][0]["content"]
        self.assertIn("+30%", system)
        self.assertIn("Always horny", system)
        self.assertIn("assistant: cumshot", system)
        self.assertNotIn("SFW MODE OVERRIDE", system)

    def test_reviewer_context_is_independent_of_character(self):
        call = Mock(return_value=SAFE)
        self.assertTrue(sfw_safe({"reply": "Hello"}, "chosen", call, "2m"))
        sent = call.call_args.kwargs["messages"]
        self.assertIn("untrusted", sent[0]["content"])
        self.assertEqual(json.loads(sent[1]["content"]), {"reply": "Hello"})


if __name__ == "__main__":
    unittest.main()
