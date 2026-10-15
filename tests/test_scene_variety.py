import json
import unittest
from unittest.mock import Mock
from worker.scene_variety import STATE_FIELDS, varied_scene


def verdict(changed=False, repeat=False, continuity=True):
    return json.dumps(dict(continuity_ok=continuity, repeat_requested=repeat,
                           meaningful_change=changed, target_satisfied=True, reason="same composition",
                           visual_state={key: "" for key in STATE_FIELDS}))


class SceneVarietyTests(unittest.TestCase):
    def run_scene(self, reviews, scenes=(), request="another picture"):
        call = Mock(side_effect=reviews)
        replan = Mock(side_effect=scenes)
        result = varied_scene("seated on bridge", {"request": request},
                              replan, call, "chosen", "2m")
        return result, call, replan

    def test_third_repetitive_candidate_is_delivered(self):
        (scene, state), call, replan = self.run_scene(
            [verdict()] * 3, ["still seated", "seated again"])
        self.assertEqual(scene, "seated again")
        self.assertTrue(state["variety_exhausted"])
        self.assertEqual(call.call_count, 3)
        self.assertEqual(replan.call_count, 2)

    def test_meaningful_change_stops_early(self):
        (scene, state), call, replan = self.run_scene(
            [verdict(), verdict(changed=True)], ["standing looking through window"])
        self.assertEqual(scene, "standing looking through window")
        self.assertEqual(state["variety_attempts"], 2)
        self.assertEqual(replan.call_count, 1)

    def test_explicit_repeat_does_not_force_change(self):
        (_, state), _, replan = self.run_scene([verdict(repeat=True)], request="repeat the same pose")
        self.assertFalse(state["variety_exhausted"])
        replan.assert_not_called()

    def test_third_attempt_cannot_override_continuity(self):
        with self.assertRaisesRegex(ValueError, "continuity"):
            self.run_scene([verdict(continuity=False)] * 3, ["Paris", "Mars"])

    def test_review_contract_covers_semantics_and_related_rooms(self):
        (_, _), call, _ = self.run_scene([verdict(changed=True)])
        instructions = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("related-room transition", instructions)
        self.assertIn("Compare semantics", instructions)
        self.assertIn("expression alone", instructions)

    def test_malformed_review_is_not_accepted_as_variety_fallback(self):
        with self.assertRaisesRegex(ValueError, "Invalid scene review"):
            self.run_scene(['{}'])

    def test_exact_duplicate_overrides_false_positive_model_review(self):
        scene = "Nyx stands at the console."
        context = {"request": "another picture", "recent_image_descriptions": [scene]}
        call = Mock(side_effect=[verdict(changed=True)] * 3)
        replan = Mock(return_value=scene)
        _, state = varied_scene(scene.upper(), context, replan, call, "chosen", "2m")
        self.assertTrue(state["variety_exhausted"])
        self.assertEqual(state["variety_attempts"], 3)
        self.assertTrue(all(item["exact_duplicate"] for item in state["variety_reviews"]))
        self.assertIn("seated", replan.call_args_list[0].args[1])
        self.assertIn("viewpoint", replan.call_args_list[1].args[1])

    def test_duplicate_wrapper_and_punctuation_normalization(self):
        from worker.scene_variety import normalize_scene
        self.assertEqual(normalize_scene("Use image 1. Requested edit: Nyx STANDS, near console!"),
                         normalize_scene("Nyx stands near console."))

    def test_model_cannot_invent_repeat_permission(self):
        scene = "Nyx stands at the console."
        _, state = varied_scene(scene, {"request": "another picture", "recent_image_descriptions": [scene]},
                                Mock(return_value=scene), Mock(return_value=verdict(changed=True, repeat=True)),
                                "chosen", "2m")
        self.assertTrue(state["variety_exhausted"])

    def test_negative_repeat_request_does_not_authorize_duplicate(self):
        from worker.scene_variety import explicit_repeat
        self.assertFalse(explicit_repeat("Don't repeat the same picture"))

    def test_explicit_repeat_allows_exact_duplicate(self):
        scene = "Nyx stands at the console."
        replan = Mock()
        _, state = varied_scene(scene, {"request": "repeat the same picture", "recent_image_descriptions": [scene]},
                                replan, Mock(return_value=verdict(repeat=True)), "chosen", "2m")
        self.assertFalse(state["variety_exhausted"])
        replan.assert_not_called()

    def test_unimplemented_target_cannot_pass_on_novelty_alone(self):
        rejected = json.loads(verdict(changed=True))
        rejected["target_satisfied"] = False
        (_, state), _, replan = self.run_scene(
            [verdict(), json.dumps(rejected), verdict(changed=True)], ["still seated", "profile view"])
        self.assertEqual(state["variety_attempts"], 3)
        self.assertEqual(replan.call_count, 2)

    def test_fixed_pose_selects_camera_target(self):
        from worker.scene_variety import repair_target
        target = repair_target("Nyx stands by console", {"request": "keep standing at the console"}, 1)
        self.assertIn("camera viewpoint", target)
        self.assertNotIn("character seated", target)

    def test_review_log_reason_redacts_environment_secrets(self):
        from unittest.mock import patch
        from worker.scene_variety import safe_reason
        with patch.dict("os.environ", {"TEST_API_KEY": "private-example-key"}):
            self.assertEqual(safe_reason("reason private-example-key"), "reason [redacted]")


class VisualStatePersistenceTests(unittest.TestCase):
    from tests.test_invalid_media_completion import InvalidMediaCompletionTests as _fixture
    setUp = _fixture.setUp
    tearDown = _fixture.tearDown
    make_job = _fixture.make_job

    def test_reply_supported_galley_state_survives_completion_and_next_dispatch(self):
        from tests.test_jobs import api
        job, unrelated = self.make_job("web")
        state = {"description": "Seated in the galley of the same ship.", "world_location": "ship",
                 "room": "galley", "posture": "seated"}
        with api.app.test_client() as client:
            response = client.post(f"/jobs/{job}/complete", json={
                "ok": True, "lease_token": "owner", "text": "Let's grab a drink in the galley.",
                "response_type": "image", "scene": state["description"], "visual_state": state})
            self.assertEqual(response.status_code, 200)
        with api.get_db() as conn:
            image = conn.execute("SELECT * FROM jobs WHERE job_type='character_image_edit'").fetchone()
            saved = json.loads(image["payload"])
            self.assertEqual(saved["visual_state"], state)
            source = conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (unrelated,))
            next_id = conn.execute("INSERT INTO jobs(chat_id,context_id,job_type,weight,payload,status) VALUES(1,?,'character_chat_intent','light',?,'queued')",
                                   (source["context_id"], source["payload"])).lastrowid
            conn.execute("UPDATE jobs SET status='done' WHERE id=?", (image["id"],))
        with api.app.test_client() as client:
            next_job = client.get('/jobs/next').get_json()
        self.assertEqual(next_job["id"], next_id)
        self.assertEqual(next_job["payload"]["recent_visual_states"], [state])
        self.assertEqual(next_job["payload"]["recent_image_descriptions"], [state["description"]])

    def test_planning_retry_preserves_shared_intention(self):
        from tests.test_jobs import api
        from tests.test_visual_moments import moment
        job,_=self.make_job('web');shared=moment()
        with api.app.test_client() as client:
            response=client.post(f'/jobs/{job}/complete',json=dict(ok=True,lease_token='owner',text='Coffee in the galley.',
                response_type='chat',shared_visual_moment=shared,
                media_plan_repair=dict(error='temporary model failure',response_type='image'))).get_json()
        with api.get_db() as conn:
            payload=json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?',(response['followup_job_id'],)).fetchone()[0])
        self.assertEqual(payload['shared_visual_moment'],shared)
        self.assertEqual(payload['drafted_reply'],'Coffee in the galley.')
