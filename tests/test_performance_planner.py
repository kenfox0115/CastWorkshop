"""Intent, performance, repair, fallback and final H3 graph contracts."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch
from test_multichar_dialogue import video_worker as worker
from worker import performance_plan as pp


class PerformancePlannerTests(unittest.TestCase):
    scene = ('Replace the clothing of both characters with futuristic military uniforms. '
             'They are having a conversation on the bridge while the rest of the crew '
             'works around them. Jordan says: "are you sure we shouldn’t run this by command?"')
    line = "are you sure we shouldn’t run this by command?"
    names = ("Captain Anika Okafor", "Jordan Reyes")

    def fallback(self, scene=None, names=None, turns=None):
        scene = self.scene if scene is None else scene
        names = names or self.names
        turns = [{"speaker": "b", "line": self.line}] if turns is None else turns
        hints = worker._multichar_performance_hints(scene, *names, turns)
        lines = {"a": None, "b": None}
        for t in turns:
            lines[t["speaker"]] = t["line"]
        return worker._fallback_multichar_scene_plan(scene, *names, lines,
            [t["speaker"] for t in turns], authoritative_turns=hints)

    def plan(self, response=None, error=None):
        with patch.object(worker, "call_ollama", return_value=response, side_effect=error) as call:
            plan = worker.build_multichar_scene_plan(*self.names, self.scene,
                {"a": None, "b": self.line}, ["b"], dialogue_turns=[{"speaker": "b", "line": self.line}])
        return plan, call

    def compile(self, plan, scene=None, names=None, turns=None):
        return worker._deterministic_multichar_h3_prompt(
            (names or self.names)[0], "adult", (names or self.names)[1], "adult",
            self.scene if scene is None else scene, {"a": None, "b": self.line}, ["b"],
            dialogue_turns=plan["turns"] if turns is None else turns, scene_plan=plan)

    def test_job_687_complete_fallback_and_compilation(self):
        plan = self.fallback()
        self.assertEqual(pp.validate(plan, plan["intent"]), [])
        self.assertTrue(plan["visual_scene"].endswith("works around them"))
        self.assertIn("Both characters are wearing futuristic military uniforms", plan["visual_scene"])
        self.assertIn("Replace the clothing", plan["image_scene"])
        self.assertIn("listening", plan["characters"]["a"]["listener"])
        self.assertIn("breathing", plan["characters"]["a"]["motion"])
        self.assertIn("blinking", plan["characters"]["b"]["motion"])
        self.assertEqual([b["audio"] for b in plan["timeline"]], ["non_vocal", "authored_dialogue", "non_vocal"])
        for beat in plan["timeline"]:
            self.assertEqual(set(beat["behavior"]), {"a", "b"})
        body = self.compile(plan)
        self.assertEqual(body.count(self.line), 1)
        self.assertNotIn("Jordan.", body)
        self.assertNotIn("Replace the clothing", body)
        self.assertIn("<Subject 2> (S1)", body)
        self.assertIn("<Subject 1> maintains", body)
        self.assertIn("listener lips remain still", body)
        self.assertIn("non-vocal ambience outside authored dialogue", body)
        self.assertNotIn("fully_preserved", body)
        self.assertTrue(pp.validate_compiled(body, plan, ["b"], plan["duration_seconds"]))
        image = worker.build_multichar_image_instruction(*self.names[:1], "adult", self.names[1], "adult", plan)
        self.assertIn("named principal characters", image)
        self.assertIn("explicitly requested background crew", image)

    def test_valid_restrained_enrichment_not_equal_to_source_hints(self):
        proposal = pp.proposal(self.fallback())
        proposal["characters"]["a"]["motion"].remove("posture")
        proposal["deliveries"] = ["neutral measured delivery"]
        proposal["opening_seconds"] = .6
        plan, call = self.plan(json.dumps(proposal))
        self.assertEqual(plan["planner_status"], "model")
        self.assertEqual(plan["turns"][0]["delivery"], "neutral measured delivery")
        self.assertNotIn("posture", plan["characters"]["a"]["motion"])
        self.assertEqual(plan["timeline"][0]["end"], .6)
        self.assertEqual(call.call_count, 1)
        self.compile(plan)

    def test_timeout_complete_fallback_without_retry(self):
        plan, call = self.plan(error=TimeoutError("private endpoint detail"))
        self.assertEqual(plan["fallback_reason"], "planner_error:TimeoutError")
        self.assertEqual(plan["repair_status"], "not_attempted")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(pp.validate(plan, plan["intent"]), [])

    def test_invalid_output_one_repair_then_complete_fallback(self):
        for response in ("not JSON", "null", "[]", "{}"):
            with self.subTest(response=response):
                plan, call = self.plan(response)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(plan["repair_status"], "failed")
                self.assertEqual(plan["planner_status"], "fallback")
                self.assertEqual(pp.validate(plan, plan["intent"]), [])
                self.assertIn("Validation errors", call.call_args.args[0])

    def test_successful_repair_preserves_valid_parts(self):
        valid = pp.proposal(self.fallback())
        valid["opening_seconds"] = .6
        invalid = deepcopy(valid)
        invalid["characters"]["a"]["listener"].append("laughing")
        plan, call = self.plan(error=[json.dumps(invalid), json.dumps(valid)])
        self.assertEqual(plan["planner_status"], "repaired")
        self.assertEqual(plan["repair_status"], "succeeded")
        self.assertEqual(plan["opening_seconds"], .6)
        self.assertEqual(call.call_count, 2)
        self.assertIn('"opening_seconds": 0.6', call.call_args.args[0])
        self.compile(plan)

    def test_failed_repair_never_leaks_invalid_enrichment(self):
        invalid = pp.proposal(self.fallback())
        invalid["characters"]["a"]["listener"] = ["touches Jordan", "speaking"]
        plan, call = self.plan(json.dumps(invalid))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(plan["repair_status"], "failed")
        self.assertNotIn("touches Jordan", self.compile(plan))

    def test_silent_scene_complete_behavior_and_no_audio_references(self):
        scene = "Anika and Jordan stand together on the bridge."
        plan = self.fallback(scene, turns=[])
        body = self.compile(plan, scene)
        self.assertEqual([b["kind"] for b in plan["timeline"]], ["opening", "ending"])
        self.assertNotIn("<Audio", body)
        self.assertNotIn("<d>", body)
        self.assertIn("silent scene", body)
        self.assertEqual(set(plan["characters"]), {"a", "b"})

    def test_explicit_empty_turns_never_reintroduce_stale_lines(self):
        with patch.object(worker, "call_ollama", side_effect=TimeoutError()):
            plan = worker.build_multichar_scene_plan("Alex", "Robin", "Both stand beside the window.",
                {"a": "stale line", "b": None}, ["a"], dialogue_turns=[])
        self.assertEqual(plan["turns"], [])
        self.assertTrue(all(b["audio"] == "non_vocal" for b in plan["timeline"]))

    def test_alternating_speakers_exact_mapping_and_listener_each_turn(self):
        scene = 'Robin says "First." Alex replies "Second." Robin says "Third."'
        turns = [{"speaker": "b", "line": "First."}, {"speaker": "a", "line": "Second."}, {"speaker": "b", "line": "Third."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        body = self.compile(plan, scene, ("Alex", "Robin"), turns)
        self.assertTrue(worker._multichar_dialogue_turns_are_exact(body, turns))
        self.assertEqual(body.count("<d>"), 3)
        self.assertIn("<Audio 1> is the voice-timbre reference for <Subject 2>", body)
        self.assertIn("<Audio 2> is the voice-timbre reference for <Subject 1>", body)
        for beat in plan["timeline"]:
            if beat["kind"] == "dialogue":
                self.assertIn("listening", beat["behavior"]["a" if beat["speaker"] == "b" else "b"])

    def test_motionless_and_immediate_speech_override_defaults(self):
        scene = 'Both remain motionless. Jordan immediately says "Ready."'
        plan = self.fallback(scene, turns=[{"speaker": "b", "line": "Ready."}])
        for char in plan["characters"].values():
            self.assertEqual(char["motion"], ["still"])
            self.assertEqual(char["listener"], ["still"])
        self.assertEqual(plan["timeline"][0]["kind"], "dialogue")
        self.assertEqual(plan["timeline"][0]["start"], 0)
        body = self.compile(plan, scene)
        self.assertNotIn("blinking", body)
        self.assertNotIn("posture adjustments", body)
        self.assertNotIn("Jordan immediately", body)
        invalid = deepcopy(plan)
        invalid["characters"]["a"]["motion"].append("gaze")
        self.assertTrue(pp.validate(invalid, plan["intent"]))

    def test_explicit_gestures_timing_camera_negation(self):
        scene = 'Both wait for two seconds. The camera remains static. Alex nods for one second then says "Yes." Robin does not wave.'
        turns = [{"speaker": "a", "line": "Yes."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        body = self.compile(plan, scene, ("Alex", "Robin"), turns)
        self.assertEqual(plan["timeline"][0]["end"], 2)
        action = next(b for b in plan["timeline"] if b["kind"] == "action_before")
        self.assertEqual(action["end"] - action["start"], 1)
        self.assertEqual(body.count("Alex nods for one second"), 1)
        self.assertIn("Robin does not wave", body)
        self.assertIn("camera remains static", body)
        image = worker.build_multichar_image_instruction("Alex", "adult", "Robin", "adult", plan)
        self.assertNotIn("nods", image)

    def test_timed_wardrobe_action_is_not_in_opening_image(self):
        scene = 'Alex says "Ready." and opens the jacket. Robin stands beside Alex.'
        turns = [{"speaker": "a", "line": "Ready."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        self.assertIn("opens the jacket", self.compile(plan, scene, ("Alex", "Robin"), turns))
        self.assertNotIn("opens the jacket", plan["image_scene"])

    def test_compiled_prompt_and_reference_mapping_reject_tampering(self):
        plan = self.fallback()
        body = self.compile(plan)
        for mutated in (body + " Jordan.", body.replace("<Audio 1>: <d>", "<Audio 2>: <d>"),
                        body + " <d>[English] Extra</d>", body + " {{unresolved}}",
                        body + " Preserve every pose motionless while blinking."):
            with self.subTest(mutated=mutated[-70:]), self.assertRaises(ValueError):
                pp.validate_compiled(mutated, plan)
        with self.assertRaises(ValueError):
            pp.validate_compiled(body, plan, ["a"], plan["duration_seconds"])
        with self.assertRaises(ValueError):
            pp.validate_compiled(body, plan, ["b"], 10)
        tampered = deepcopy(plan)
        tampered["intent"]["source"] = "changed request"
        with self.assertRaises(ValueError):
            self.compile(tampered)

    def test_overfull_authored_timeline_rejected_not_truncated(self):
        turns = [{"speaker": "b", "line": " ".join(["word"] * 40)}]
        with self.assertRaisesRegex(ValueError, "10-second"):
            self.fallback('Jordan says "' + turns[0]["line"] + '"', turns=turns)

    def test_actual_graph_mapping_validation_before_submission(self):
        cc = worker.comfy_client
        plan = self.fallback()
        body = self.compile(plan)
        response = Mock()
        response.json.return_value = {"p": {"status": {"status_str": "success"},
            "outputs": {"92": {"videos": [{"filename": "video.mp4"}]}}}}
        with patch.object(cc, "submit_prompt", return_value="p") as submit, \
             patch.object(cc.requests, "get", return_value=response), \
             patch.object(cc, "fetch_image_bytes", return_value=b"video"), patch.object(cc, "free_vram"):
            cc.generate_multichar_voice_reference_video("a.png", "b.png", "scene.png", "a.flac", "b.flac", body,
                duration_seconds=plan["duration_seconds"], audio_reference_speakers=["b"], performance_plan=plan)
            graph = submit.call_args.args[0]
            self.assertEqual(graph["141"]["inputs"]["audio"], "b.flac")
            self.assertNotIn("146", graph)
            submit.reset_mock()
            with self.assertRaises(ValueError):
                cc.generate_multichar_voice_reference_video("a.png", "b.png", "scene.png", "a.flac", "b.flac", body,
                    duration_seconds=plan["duration_seconds"], audio_reference_speakers=["a"], performance_plan=plan)
            submit.assert_not_called()

    def test_listener_silence_does_not_suppress_other_speaker(self):
        scene = 'Alex says "Hello." while Robin remains silent.'
        turns = [{"speaker": "a", "line": "Hello."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        self.assertIn("Hello.</d>", self.compile(plan, scene, ("Alex", "Robin"), turns))
        self.assertEqual(pp.silent_cast(scene, {"a": "Alex", "b": "Robin"}), {"b"})

    def test_global_silence_beats_stale_payload_and_compiles_without_audio(self):
        scene = 'Both remain silent beside the window.'
        turns = [{"speaker": "a", "line": "Stale."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        body = self.compile(plan, scene, ("Alex", "Robin"), turns)
        self.assertEqual(plan["turns"], [])
        self.assertNotIn("<Audio", body)
        self.assertNotIn("Stale.", body)

    def test_source_changes_and_invalid_timeline_fail_validation(self):
        original = self.fallback()
        for field, value in (("visual_scene", "An invented beach"), ("camera", "Cut to closeup"),
                             ("ambience", "People talking"), ("image_scene", "An invented prop")):
            altered = deepcopy(original)
            altered[field] = value
            self.assertTrue(pp.validate(altered, original["intent"]))
        altered = deepcopy(original)
        altered["timeline"][1]["speaker"] = "a"
        self.assertTrue(pp.validate(altered, original["intent"]))
        altered = deepcopy(original)
        altered["timeline"][1]["behavior"].pop("a")
        self.assertTrue(pp.validate(altered, original["intent"]))
        altered = deepcopy(original)
        altered["timeline"] += [deepcopy(altered["timeline"][1])]
        self.assertTrue(pp.validate(altered, original["intent"]))

    def test_silent_authored_actions_have_timed_beats_without_duplication(self):
        scene = 'Alex opens the door. Robin walks inside.'
        plan = self.fallback(scene, ("Alex", "Robin"), turns=[])
        actions = [b for b in plan["timeline"] if b["kind"] == "scene_action"]
        self.assertEqual([b["text"] for b in actions], ["Alex opens the door", "Robin walks inside"])
        self.assertTrue(all(b["audio"] == "non_vocal" for b in actions))
        body = self.compile(plan, scene, ("Alex", "Robin"), turns=[])
        self.assertEqual(body.count("Alex opens the door"), 1)
        self.assertEqual(body.count("Robin walks inside"), 1)

    def test_other_listener_authored_action_after_last_line_is_not_moved_before(self):
        scene = 'Alex says "Yes." Then Robin nods.'
        turns = [{"speaker": "a", "line": "Yes."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        body = self.compile(plan, scene, ("Alex", "Robin"), turns)
        self.assertLess(body.index("Yes.</d>"), body.index("Robin nods"))

    def test_repair_timeout_status_and_no_third_call(self):
        plan, call = self.plan(error=["{}", TimeoutError()])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(plan["fallback_reason"], "repair_error:TimeoutError")
        self.assertEqual(plan["repair_status"], "failed")
        self.assertEqual(pp.validate(plan, plan["intent"]), [])

    def test_distinct_actions_with_same_verb_are_not_discarded(self):
        scene = 'Alex opens the door. Robin stands inside. Alex opens the window then says "Ready."'
        turns = [{"speaker": "a", "line": "Ready."}]
        plan = self.fallback(scene, ("Alex", "Robin"), turns)
        body = self.compile(plan, scene, ("Alex", "Robin"), turns)
        self.assertEqual(body.count("Alex opens the door"), 1)
        self.assertEqual(body.count("Alex opens the window"), 1)


if __name__ == "__main__":
    unittest.main()
