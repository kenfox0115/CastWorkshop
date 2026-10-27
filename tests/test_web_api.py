"""Local web context and chat contract tests."""
import base64,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_delivery
from vps import artifacts
api=test_delivery.api

class WebApiTests(unittest.TestCase):
    setUp=test_delivery.DeliveryTests.setUp
    tearDown=test_delivery.DeliveryTests.tearDown

    def character(self,name="Avery"):
        with api.get_db() as conn:
            return conn.execute("""INSERT INTO characters(name,concept,system_prompt,base_image_path,voice_sample_path)
                VALUES(?,?,?,?,?)""",(name,"private concept","Be conversational.","secret/image.png","secret/voice.wav")).lastrowid

    def test_bootstrap_is_sanitized_and_transport_scoped(self):
        character_id=self.character()
        with api.get_db() as conn:
            conn.execute("INSERT INTO sessions(character_id,chat_id,state) VALUES(?,?,?)",(character_id,8675309,"active"))
            conn.execute("INSERT INTO jobs(chat_id,job_type,payload,status) VALUES(?,?,?,?)",(8675309,"character_image",'{"prompt":"private prompt"}',"queued"))
        with api.app.test_client() as client:
            response=client.get("/api/v1/bootstrap")
        data=response.get_json()
        self.assertEqual(response.status_code,200)
        self.assertEqual(data["queue"]["queued"],0)
        self.assertEqual(data["characters"][0]["name"],"Avery")
        self.assertEqual(data["active_character_id"],character_id)
        for secret in ("8675309","private concept","private prompt","secret/image.png","secret/voice.wav"):
            self.assertNotIn(secret,response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_select_send_and_complete_web_chat_without_delivery(self):
        character_id=self.character()
        with api.app.test_client() as client:
            selected=client.put("/api/v1/active-character",json={"character_id":character_id})
            self.assertEqual(selected.status_code,200)
            sent=client.post("/api/v1/messages",json={"content":"Hello there"})
            self.assertEqual(sent.status_code,202)
            job_id=sent.get_json()["job_id"]
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(claimed["id"],job_id)
            self.assertIsInstance(claimed["context_id"],int)
            self.assertTrue(claimed["payload"]["allow_media"])
            done=client.post(f"/jobs/{job_id}/complete",json={
                "lease_token":claimed["lease_token"],"ok":True,"text":"Hello. Good to see you.",
                "response_type":"chat",
            })
            self.assertEqual(done.status_code,200)
            result=client.get(f"/api/v1/jobs/{job_id}").get_json()
            self.assertEqual(result["status"],"done")
            bootstrap=client.get("/api/v1/bootstrap").get_json()
        self.assertEqual([m["role"] for m in bootstrap["messages"]],["user","assistant"])
        self.assertEqual(bootstrap["messages"][-1]["content"],"Hello. Good to see you.")
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT transport FROM contexts JOIN jobs ON contexts.id=jobs.context_id WHERE jobs.id=?",(job_id,)).fetchone()[0],"web")

    def test_web_and_telegram_share_selection_and_chat_history(self):
        first=self.character("Avery");second=self.character("Blair")
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":first})
            with api.get_db() as conn:
                self.assertEqual(api.get_active_character(conn,777)["id"],first)
                api.record_message(conn,first,777,"user","Telegram hello",kind="chat")
                api.record_message(conn,first,777,"assistant","Telegram reply",kind="chat")
            shared=client.get("/api/v1/bootstrap").get_json()
            self.assertEqual([m["content"] for m in shared["messages"]],["Telegram hello","Telegram reply"])
            self.assertEqual({m["source"] for m in shared["messages"]},{"telegram"})
            sent=client.post("/api/v1/messages",json={"content":"Continue here"}).get_json()
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(claimed["id"],sent["job_id"])
            self.assertEqual([turn["content"] for turn in claimed["payload"]["conversation_turns"]],["Telegram hello","Telegram reply"])
            client.post(f"/jobs/{claimed['id']}/complete",json={"lease_token":claimed["lease_token"],"ok":True,"text":"Web reply","response_type":"chat"})
            with api.get_db() as conn:
                api.record_message(conn, first, 777, "user", "Telegram continues", kind="chat")
                telegram_job = api.enqueue_job(conn, 777, "character_chat_intent", "light", {
                    "character_id": first, "message": "Telegram continues",
                })
            telegram_claim = client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(telegram_claim["id"], telegram_job)
            shared_turns = [turn["content"] for turn in telegram_claim["payload"]["conversation_turns"]]
            self.assertIn("Continue here", shared_turns)
            self.assertIn("Web reply", shared_turns)
        api.handle_message(777,"/characters")
        with api.get_db() as conn:
            picker=api.get_in_progress_session(conn,777);choices=json.loads(picker["pending_data"])
            choice=next(index for index,item in enumerate(choices,1) if item["id"]==second)
        api.handle_message(777,str(choice))
        with api.app.test_client() as client:after=client.get("/api/v1/bootstrap").get_json()
        self.assertEqual(after["active_character_id"],second)

    def test_telegram_media_is_persisted_for_shared_web_timeline(self):
        character_id=self.character("Avery")
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.get_db() as conn:
                api.set_shared_active_character(conn,character_id)
                job_id=api.enqueue_job(conn,777,"character_image_edit","heavy",{"character_id":character_id,"prompt":"portrait","reference_image":"secret/image.png"})
            with api.app.test_client() as client:
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                done=client.post(f"/jobs/{job_id}/complete",json={"lease_token":claimed["lease_token"],"ok":True,"image_b64":base64.b64encode(b"telegram image").decode()})
                self.assertEqual(done.status_code,200)
                timeline=client.get("/api/v1/bootstrap").get_json()["messages"]
                self.assertEqual(timeline[-1]["source"],"telegram")
                artifact=timeline[-1]["artifacts"][0]
                served=client.get(f"/api/v1/artifacts/{artifact['id']}")
                self.assertEqual(served.data,b"telegram image")
                served.close()
            with api.get_db() as conn:
                self.assertEqual(conn.execute("SELECT transport FROM contexts WHERE id=(SELECT context_id FROM jobs WHERE id=?)",(job_id,)).fetchone()[0],"telegram")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries WHERE job_id=?",(job_id,)).fetchone()[0],1)

    def test_spontaneous_image_chains_to_same_reply(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir, patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                client.put("/api/v1/active-character",json={"character_id":character_id})
                first=client.post("/api/v1/messages",json={"content":"Surprise me"}).get_json()
                intent=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{first['job_id']}/complete",json={
                    "lease_token":intent["lease_token"],"ok":True,
                    "text":"I took this just for you.","response_type":"image",
                    "scene":"smiling beside a sunny window",
                })
                result=client.get(f"/api/v1/jobs/{first['job_id']}").get_json()["result"]
                self.assertIsInstance(result["followup_job_id"],int)
                pending=client.get("/api/v1/bootstrap").get_json()
                self.assertEqual([m["role"] for m in pending["messages"]],["user"])
                self.assertEqual(pending["active_job_id"],result["followup_job_id"])
                media=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(media["job_type"],"character_image_edit")
                self.assertEqual(media["context_id"],intent["context_id"])
                client.post(f"/jobs/{media['id']}/complete",json={
                    "lease_token":media["lease_token"],"ok":True,
                    "image_b64":base64.b64encode(b"photo").decode(),
                })
                messages=client.get("/api/v1/bootstrap").get_json()["messages"]
            self.assertEqual([m["role"] for m in messages],["user","assistant"])
            self.assertEqual(messages[-1]["content"],"I took this just for you.")
            self.assertEqual(messages[-1]["artifacts"][0]["kind"],"image")

    def test_image_completion_creates_scoped_durable_artifact(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir, patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                client.put("/api/v1/active-character",json={"character_id":character_id})
                sent=client.post("/api/v1/media/image",json={"content":"standing by a window"})
                self.assertEqual(sent.status_code,202)
                job_id=sent.get_json()["job_id"]
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(claimed["job_type"],"character_image_edit")
                self.assertIn("standing by a window",claimed["payload"]["prompt"])
                completed=client.post(f"/jobs/{job_id}/complete",json={
                    "lease_token":claimed["lease_token"],"ok":True,
                    "image_b64":base64.b64encode(b"png data").decode(),
                })
                self.assertEqual(completed.status_code,200)
                bootstrap=client.get("/api/v1/bootstrap").get_json()
                artifact=bootstrap["messages"][-1]["artifacts"][0]
                served=client.get(f"/api/v1/artifacts/{artifact['id']}")
                downloaded=client.get(f"/api/v1/artifacts/{artifact['id']}?download=1")
            self.assertEqual(served.status_code,200)
            self.assertEqual(served.data,b"png data")
            self.assertEqual(downloaded.status_code,200)
            self.assertEqual(downloaded.data,b"png data")
            self.assertIn("attachment",downloaded.headers["Content-Disposition"])
            self.assertIn(f"castworkshop-{artifact['id']}.png",downloaded.headers["Content-Disposition"])
            served.close()
            downloaded.close()
            self.assertEqual(artifact["kind"],"image")
            self.assertEqual(len(list(Path(media_dir).glob("*.png"))),1)
            with api.get_db() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)

    def test_completed_video_can_enqueue_a_last_frame_continuation(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                client.put("/api/v1/active-character",json={"character_id":character_id})
                original=client.post("/api/v1/media/video",json={"content":"walks onto the balcony"}).get_json()
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                completed=client.post(f"/jobs/{original['job_id']}/complete",json={
                    "lease_token":claimed["lease_token"],"ok":True,
                    "video_b64":base64.b64encode(b"video bytes").decode(),
                })
                artifact_id=completed.get_json()["artifact_id"]
                original_relative_path=next(Path(media_dir).glob("*.mp4")).name
                continued=client.post(f"/api/v1/artifacts/{artifact_id}/continue",json={
                    "content":"turns toward the ocean and smiles"
                })
                self.assertEqual(continued.status_code,202)
                followup=client.get("/jobs/next?worker_id=test").get_json()
                followup_done=client.post(f"/jobs/{followup['id']}/complete",json={
                    "lease_token":followup["lease_token"],"ok":True,
                    "video_b64":base64.b64encode(b"continued video bytes").decode(),
                })
                continued_artifact_id=followup_done.get_json()["artifact_id"]
                chain=client.get(f"/api/v1/artifacts/{continued_artifact_id}/video-chain")
            self.assertEqual(followup["job_type"],"character_video")
            self.assertEqual(followup["payload"]["continuation_artifact_id"],artifact_id)
            self.assertEqual(followup["payload"]["continuation_parent_job_id"],original["job_id"])
            self.assertEqual(followup["payload"]["continuation_relative_path"],original_relative_path)
            self.assertEqual(followup["payload"]["motion_prompt"],"turns toward the ocean and smiles")
            self.assertEqual(chain.status_code,200)
            self.assertEqual([item["id"] for item in chain.get_json()["artifacts"]],
                             [artifact_id,continued_artifact_id])

    def test_continuation_rejects_non_video_artifact(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                client.put("/api/v1/active-character",json={"character_id":character_id})
                sent=client.post("/api/v1/media/image",json={"content":"portrait"}).get_json()
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                completed=client.post(f"/jobs/{sent['job_id']}/complete",json={
                    "lease_token":claimed["lease_token"],"ok":True,
                    "image_b64":base64.b64encode(b"image").decode(),
                })
                response=client.post(f"/api/v1/artifacts/{completed.get_json()['artifact_id']}/continue",json={"content":"move"})
            self.assertEqual(response.status_code,404)

    def test_two_character_continuation_preserves_named_dialogue(self):
        first=self.character("Avery");second=self.character("Blair")
        scene='Avery says "Ready." Blair replies "Always."'
        turns=[{"speaker":"a","line":"Ready."},{"speaker":"b","line":"Always."}]
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                original=client.post("/api/v1/media/multi-video",json={
                    "character_a_id":first,"character_b_id":second,"content":scene,
                    "dialogue_turns":turns,"confirmed":True,
                }).get_json()
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                completed=client.post(f"/jobs/{original['job_id']}/complete",json={
                    "lease_token":claimed["lease_token"],"ok":True,
                    "video_b64":base64.b64encode(b"original video").decode(),
                }).get_json()
                continued=client.post(f"/api/v1/artifacts/{completed['artifact_id']}/continue",json={
                    "content":'Avery says "Come closer." Blair replies "Gladly."',
                })
                self.assertEqual(continued.status_code,200)
                preview=continued.get_json()
                self.assertTrue(preview["requires_speaker_confirmation"])
                self.assertEqual(preview["dialogue_turns"],turns := [
                    {"speaker":"a","line":"Come closer."},{"speaker":"b","line":"Gladly."},
                ])
                continued=client.post(f"/api/v1/artifacts/{completed['artifact_id']}/continue",json={
                    "content":preview["scene_text"],"dialogue_turns":turns,"confirmed":True,
                })
                self.assertEqual(continued.status_code,202)
                followup=client.get("/jobs/next?worker_id=test").get_json()
        self.assertEqual(followup["payload"]["dialogue_turns"],[
            {"speaker":"a","line":"Come closer."},{"speaker":"b","line":"Gladly."},
        ])
        self.assertEqual(followup["payload"]["spoken_line_a"],"Come closer.")
        self.assertEqual(followup["payload"]["spoken_line_b"],"Gladly.")

    def test_two_character_continuation_requires_ambiguous_speaker_choice(self):
        first=self.character("John");second=self.character("Frankie")
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                original=client.post("/api/v1/media/multi-video",json={
                    "character_a_id":first,"character_b_id":second,
                    "content":'John says "Hello."',
                    "dialogue_turns":[{"speaker":"a","line":"Hello."}],"confirmed":True,
                }).get_json()
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                completed=client.post(f"/jobs/{original['job_id']}/complete",json={
                    "lease_token":claimed["lease_token"],"ok":True,
                    "video_b64":base64.b64encode(b"original video").decode(),
                }).get_json()
                preview=client.post(f"/api/v1/artifacts/{completed['artifact_id']}/continue",json={
                    "content":'She smiles and says "Which one is yours?"',
                }).get_json()
                self.assertIsNone(preview["dialogue_turns"][0]["speaker"])
                self.assertIsNone(client.get("/jobs/next?worker_id=test").get_json())
                confirmed=client.post(f"/api/v1/artifacts/{completed['artifact_id']}/continue",json={
                    "content":preview["scene_text"],
                    "dialogue_turns":[{"speaker":"b","line":"Which one is yours?"}],
                    "confirmed":True,
                })
                self.assertEqual(confirmed.status_code,202)
                followup=client.get("/jobs/next?worker_id=test").get_json()
        self.assertEqual(followup["payload"]["dialogue_turns"],[
            {"speaker":"b","line":"Which one is yours?"},
        ])

    def test_scene_continuity_is_explicit_character_scoped_and_applied(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            empty=client.get("/api/v1/scene").get_json()
            self.assertEqual(empty,{"enabled":False,"description":"","revision":0,"anchor_artifact_id":None,"anchor_ready":False})
            saved=client.put("/api/v1/scene",json={"enabled":True,"description":"Rainy rooftop at night; red coat."})
            self.assertEqual(saved.status_code,200)
            self.assertTrue(saved.get_json()["enabled"])
            sent=client.post("/api/v1/media/image",json={"content":"waves at the camera"})
            claimed=client.get("/jobs/next?worker_id=test").get_json()
        self.assertEqual(sent.status_code,202)
        self.assertIn("Rainy rooftop at night",claimed["payload"]["prompt"])
        self.assertIn("waves at the camera",claimed["payload"]["prompt"])
        self.assertEqual(claimed["payload"]["scene_context"],"Rainy rooftop at night; red coat.")
        self.assertGreaterEqual(claimed["payload"]["scene_revision"],1)

    def test_scene_anchor_candidate_approval_and_future_reference(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                client.put("/api/v1/active-character",json={"character_id":character_id})
                client.put("/api/v1/scene",json={"enabled":True,"description":"Warm library with green walls."})
                queued=client.post("/api/v1/scene/anchor-candidate")
                self.assertEqual(queued.status_code,202)
                candidate=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(candidate["job_type"],"character_image_edit")
                self.assertEqual(candidate["payload"]["reference_image"],"secret/image.png")
                completed=client.post(f"/jobs/{candidate['id']}/complete",json={
                    "lease_token":candidate["lease_token"],"ok":True,
                    "image_b64":base64.b64encode(b"anchor image").decode(),
                })
                artifact_id=completed.get_json()["artifact_id"]
                approval=client.post("/api/v1/scene/anchor",json={"artifact_id":artifact_id})
                self.assertEqual(approval.status_code,202)
                promotion=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(promotion["job_type"],"promote_scene_anchor")
                client.post(f"/jobs/{promotion['id']}/complete",json={
                    "lease_token":promotion["lease_token"],"ok":True,"text":"scene_anchor_1.png",
                })
                scene=client.get("/api/v1/scene").get_json()
                self.assertTrue(scene["anchor_ready"])
                self.assertEqual(scene["anchor_artifact_id"],artifact_id)
                request=client.post("/api/v1/media/image",json={"content":"sits in the armchair"})
                self.assertEqual(request.status_code,202)
                image=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(image["payload"]["reference_image"],"scene_anchor_1.png")
            self.assertTrue(image["payload"]["using_scene_anchor"])

    def test_changing_scene_description_invalidates_visual_anchor(self):
        character_id=self.character()
        with api.get_db() as conn:
            conn.execute("INSERT INTO character_scenes(character_id,enabled,description,anchor_artifact_id,anchor_reference_image) VALUES(?,1,?,NULL,?)",(character_id,"Old room","old_anchor.png"))
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            changed=client.put("/api/v1/scene",json={"enabled":True,"description":"New garden"}).get_json()
        self.assertFalse(changed["anchor_ready"])

    def test_scene_requires_description_when_enabled(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            response=client.put("/api/v1/scene",json={"enabled":True,"description":"  "})
        self.assertEqual(response.status_code,422)

    @patch("vps.web_api.model_readiness")
    def test_model_readiness_endpoint_uses_current_model(self,readiness):
        readiness.return_value={"manifest_version":1,"models":[],"errors":{}}
        with api.app.test_client() as client:
            response=client.get("/api/v1/models/readiness")
        self.assertEqual(response.status_code,200)
        self.assertEqual(readiness.call_args.kwargs["ollama_model"],"qwen3-14b-64k")

    def test_message_requires_selection(self):
        with api.app.test_client() as client:
            response=client.post("/api/v1/messages",json={"content":"Hello"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.get_json()["error"]["code"],"state_conflict")

    def test_two_character_image_uses_both_references(self):
        first=self.character("Avery");second=self.character("Blair")
        with api.app.test_client() as client:
            sent=client.post("/api/v1/media/multi-image",json={"character_a_id":first,"character_b_id":second,"content":"walking together at sunset"})
            self.assertEqual(sent.status_code,202)
            claimed=client.get("/jobs/next?worker_id=test").get_json()
        self.assertEqual(claimed["job_type"],"multi_character_image")
        self.assertEqual(claimed["payload"]["char_a"]["name"],"Avery")
        self.assertEqual(claimed["payload"]["char_b"]["name"],"Blair")
        self.assertEqual(claimed["payload"]["scene_text"],"walking together at sunset")
        self.assertIsInstance(claimed["context_id"],int)

    def test_two_character_video_requires_and_preserves_speaker_confirmation(self):
        first=self.character("Avery");second=self.character("Blair")
        with api.get_db() as conn:
            scene_id=conn.execute("INSERT INTO scenes(name,description) VALUES(?,?)",("Studio","a quiet daylight studio with tall windows")).lastrowid
            conn.execute("INSERT INTO scene_assets(scene_id,aspect_mode,anchor_reference_image) VALUES(?,?,?)",(scene_id,"cinematic","scene/studio-wide.png"))
            api.set_setting(conn,"active_scene_id",str(scene_id));api.set_setting(conn,"aspect_mode","cinematic")
        scene='Avery says "Ready when you are." Blair replies "Let us go."'
        base={"character_a_id":first,"character_b_id":second,"content":scene}
        with api.app.test_client() as client:
            preview=client.post("/api/v1/media/multi-video/preview",json=base)
            self.assertEqual(preview.status_code,200)
            turns=preview.get_json()["dialogue_turns"]
            self.assertEqual(turns,[{"speaker":"a","line":"Ready when you are."},{"speaker":"b","line":"Let us go."}])
            rejected=client.post("/api/v1/media/multi-video",json={**base,"dialogue_turns":turns})
            self.assertEqual(rejected.status_code,409)
            sent=client.post("/api/v1/media/multi-video",json={**base,"dialogue_turns":turns,"confirmed":True})
            self.assertEqual(sent.status_code,202)
            claimed=client.get("/jobs/next?worker_id=test").get_json()
        self.assertEqual(claimed["job_type"],"multi_character_video")
        self.assertEqual(claimed["payload"]["dialogue_turns"],turns)
        self.assertEqual(claimed["payload"]["char_a"]["voice_sample"],"secret/voice.wav")
        self.assertEqual(claimed["payload"]["char_b"]["voice_sample"],"secret/voice.wav")
        self.assertEqual(claimed["payload"]["scene_id"],scene_id)
        self.assertEqual(claimed["payload"]["scene_context"],"a quiet daylight studio with tall windows")
        self.assertEqual(claimed["payload"]["scene_aspect_mode"],"cinematic")
        self.assertEqual(claimed["payload"]["scene_reference_image"],"scene/studio-wide.png")

    def test_two_character_video_rejects_changed_dialogue_after_preview(self):
        first=self.character("Avery");second=self.character("Blair")
        body={"character_a_id":first,"character_b_id":second,"content":'Avery says "Original line."',"confirmed":True,"dialogue_turns":[{"speaker":"a","line":"Changed line."}]}
        with api.app.test_client() as client:response=client.post("/api/v1/media/multi-video",json=body)
        self.assertEqual(response.status_code,422)

    def test_web_character_creation_reaches_ready_with_candidates(self):
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)):
            with api.app.test_client() as client:
                made=client.post("/api/v1/characters",json={"name":"Blair","concept":"adult photographer","personality_notes":"curious and warm"})
                self.assertEqual(made.status_code,201);character_id=made.get_json()["id"]
                profile=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{profile['id']}/complete",json={"lease_token":profile["lease_token"],"ok":True,"text":"Profile"})
                looks=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{looks['id']}/complete",json={"lease_token":looks["lease_token"],"ok":True,"images_b64":[base64.b64encode(b"one").decode(),base64.b64encode(b"two").decode()],"filenames":["one.png","two.png"]})
                state=client.get(f"/api/v1/characters/{character_id}/creation-state").get_json()
                self.assertEqual(state["stage"],"awaiting_choice");self.assertEqual(len(state["candidate_artifact_ids"]),2)
                self.assertEqual(client.post(f"/api/v1/characters/{character_id}/look-candidates").status_code,202)
                replacement_looks=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(replacement_looks["job_type"],"character_image_batch")
                client.post(f"/jobs/{replacement_looks['id']}/complete",json={"lease_token":replacement_looks["lease_token"],"ok":True,"images_b64":[base64.b64encode(b"three").decode()],"filenames":["three.png"]})
                client.post(f"/api/v1/characters/{character_id}/look-selection",json={"choice":0})
                promote=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{promote['id']}/complete",json={"lease_token":promote["lease_token"],"ok":True,"text":"reference.png"})
                voices=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{voices['id']}/complete",json={"lease_token":voices["lease_token"],"ok":True,"audios_b64":[base64.b64encode(b"a").decode(),base64.b64encode(b"b").decode()],"audio_files":[{"filename":"a.wav"},{"filename":"b.wav"}]})
                state=client.get(f"/api/v1/characters/{character_id}/creation-state").get_json()
                self.assertEqual(state["stage"],"awaiting_voice_choice");self.assertEqual(len(state["candidate_artifact_ids"]),2)
                rerolled=client.post(f"/api/v1/characters/{character_id}/voice-candidates")
                self.assertEqual(rerolled.status_code,202)
                replacement=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(replacement["job_type"],"voice_sample_batch")
                client.post(f"/jobs/{replacement['id']}/complete",json={"lease_token":replacement["lease_token"],"ok":True,"audios_b64":[base64.b64encode(b"c").decode()],"audio_files":[{"filename":"c.wav"}]})
                client.post(f"/api/v1/characters/{character_id}/voice-selection",json={"choice":0})
                voice=client.get("/jobs/next?worker_id=test").get_json()
                client.post(f"/jobs/{voice['id']}/complete",json={"lease_token":voice["lease_token"],"ok":True,"text":"voice.wav"})
                self.assertEqual(client.get(f"/api/v1/characters/{character_id}/creation-state").get_json()["stage"],"ready")
            with api.get_db() as conn:self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)

    def test_unfinished_creation_is_resumable_and_blocks_other_work(self):
        existing=self.character("Existing")
        with api.app.test_client() as client:
            made=client.post("/api/v1/characters",json={"name":"Blair","concept":"adult photographer","personality_notes":"curious and warm"})
            character_id=made.get_json()["id"]
            bootstrap=client.get("/api/v1/bootstrap").get_json()
            self.assertEqual(bootstrap["active_creation"]["character_id"],character_id)
            self.assertEqual(bootstrap["active_job"]["type"],"profile_generate")
            self.assertEqual(client.put("/api/v1/active-character",json={"character_id":existing}).status_code,409)
            self.assertEqual(client.post("/api/v1/messages",json={"content":"hello"}).status_code,409)
            self.assertEqual(client.post("/api/v1/media/image",json={"content":"portrait"}).status_code,409)

    def test_cancel_queued_chat_removes_prompt_and_unlocks_page(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            job_id=client.post("/api/v1/messages",json={"content":"cancel this"}).get_json()["job_id"]
            self.assertEqual(client.post(f"/api/v1/jobs/{job_id}/cancel").status_code,204)
            bootstrap=client.get("/api/v1/bootstrap").get_json()
        self.assertIsNone(bootstrap["active_job"]);self.assertEqual(bootstrap["messages"],[])

    def test_failed_job_exposes_progress_and_retries_without_duplicate_prompt(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            original=client.post("/api/v1/media/image",json={"content":"portrait at sunset"}).get_json()["job_id"]
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            client.post(f"/jobs/{original}/lease",json={"lease_token":claimed["lease_token"],"progress":{"phase":"rendering_image"}})
            failed=client.post(f"/jobs/{original}/complete",json={"lease_token":claimed["lease_token"],"ok":False,"text":"RuntimeError: renderer unavailable"})
            self.assertEqual(failed.status_code,200)
            state=client.get("/api/v1/bootstrap").get_json()
            self.assertEqual(state["recent_job"]["progress"]["phase"],"rendering_image")
            self.assertEqual(state["recent_job"]["failure"],"RuntimeError: renderer unavailable")
            self.assertTrue(state["recent_job"]["can_retry"])
            retried=client.post(f"/api/v1/jobs/{original}/retry")
            self.assertEqual(retried.status_code,202)
            replacement=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(replacement["payload"]["prompt"],claimed["payload"]["prompt"])
            self.assertEqual(replacement["payload"]["user_message_id"],claimed["payload"]["user_message_id"])
            self.assertEqual(replacement["payload"]["retry_of_job_id"],original)
            self.assertEqual(len(client.get("/api/v1/bootstrap").get_json()["messages"]),1)

    def test_restart_selection_step_regenerates_candidates_and_preserves_assets(self):
        for promotion,generation,stage,label in (
            ("promote_voice","voice_sample_batch","voice_generating","voice"),
            ("promote_reference","character_image_batch","image_queued","look"),
        ):
            with self.subTest(step=label),api.app.test_client() as client:
                character_id=self.character()
                client.get("/api/v1/bootstrap")
                with api.get_db() as conn:
                    cid=conn.execute("SELECT id FROM contexts WHERE transport='web'").fetchone()[0]
                    source=api.enqueue_job(conn,0,generation,"heavy",{"character_id":character_id,"prompt":"original candidate script","voice_description":"warm voice"},context_id=cid)
                    conn.execute("UPDATE jobs SET status='done' WHERE id=?",(source,))
                    original=api.enqueue_job(conn,0,promotion,"heavy",{"character_id":character_id,"filename":"truncated.flac"},context_id=cid)
                    conn.execute("INSERT INTO sessions(character_id,chat_id,context_id,state) VALUES(?,0,?,?)",(character_id,cid,"promoting_voice" if label=="voice" else "promoting_reference"))
                claimed=client.get("/jobs/next?worker_id=test").get_json()
                self.assertEqual(claimed["id"],original)
                failed=client.post(f"/jobs/{original}/complete",json={"lease_token":claimed["lease_token"],"ok":False,"text":"boundary missing"})
                self.assertEqual(failed.status_code,200)
                detail=client.get(f"/api/v1/jobs/{original}").get_json()
                self.assertEqual(detail["restart_step"],label)
                restarted=client.post(f"/api/v1/jobs/{original}/retry",json={"scope":"step"})
                self.assertEqual(restarted.status_code,202)
                new_id=restarted.get_json()["job_id"]
                self.assertEqual(client.post(f"/api/v1/jobs/{original}/retry",json={"scope":"step"}).status_code,409)
                creation=client.get(f"/api/v1/characters/{character_id}/creation-state").get_json()
                self.assertEqual(creation["stage"],stage)
                with api.get_db() as conn:
                    row=conn.execute("SELECT * FROM jobs WHERE id=?",(new_id,)).fetchone()
                    payload=json.loads(row["payload"])
                    self.assertEqual(row["job_type"],generation)
                    self.assertEqual(payload["prompt"],"original candidate script")
                    self.assertEqual(payload["retry_of_job_id"],original)
                    self.assertNotIn("filename",payload)
                    char=conn.execute("SELECT * FROM characters WHERE id=?",(character_id,)).fetchone()
                    self.assertEqual(char["base_image_path"],"secret/image.png")
                    self.assertEqual(char["voice_sample_path"],"secret/voice.wav")
                    # Finish this subtest without leaving active work for the next step.
                    conn.execute("UPDATE jobs SET status='done' WHERE id=?",(new_id,))
                    conn.execute("DELETE FROM sessions WHERE character_id=?",(character_id,))

    def test_restart_failed_voice_generation_returns_new_candidates_to_picker(self):
        character_id=self.character()
        with tempfile.TemporaryDirectory() as media_dir,patch.object(artifacts,"MEDIA_ROOT",Path(media_dir)),api.app.test_client() as client:
            client.get("/api/v1/bootstrap")
            with api.get_db() as conn:
                cid=conn.execute("SELECT id FROM contexts WHERE transport='web'").fetchone()[0]
                original=api.enqueue_job(conn,0,"voice_sample_batch","heavy",{"character_id":character_id,"prompt":"full passage"},context_id=cid)
                conn.execute("UPDATE jobs SET status='failed',result='{}' WHERE id=?",(original,))
            restarted=client.post(f"/api/v1/jobs/{original}/retry",json={"scope":"step"})
            self.assertEqual(restarted.status_code,202)
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(claimed["id"],restarted.get_json()["job_id"])
            done=client.post(f"/jobs/{claimed['id']}/complete",json={"lease_token":claimed["lease_token"],"ok":True,"audios_b64":[base64.b64encode(b"fresh recording").decode()],"audio_files":[{"filename":"fresh.wav"}]})
            self.assertEqual(done.status_code,200)
            state=client.get(f"/api/v1/characters/{character_id}/creation-state").get_json()
            self.assertEqual(state["stage"],"awaiting_voice_choice")
            self.assertEqual(state["candidate_count"],1)
            self.assertEqual(len(state["candidate_artifact_ids"]),1)
            self.assertEqual(client.post(f"/api/v1/characters/{character_id}/voice-selection",json={"choice":0}).status_code,202)
            promotion=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(promotion["payload"]["filename"],"fresh.wav")

    def test_restart_step_requires_original_request_in_same_context(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.get("/api/v1/bootstrap")
            with api.get_db() as conn:
                cid=conn.execute("SELECT id FROM contexts WHERE transport='web'").fetchone()[0]
                other=api.enqueue_job(conn,777,"voice_sample_batch","heavy",{"character_id":character_id,"prompt":"private other context"})
                conn.execute("UPDATE jobs SET status='done' WHERE id=?",(other,))
                original=api.enqueue_job(conn,0,"promote_voice","heavy",{"character_id":character_id,"filename":"bad.flac"},context_id=cid)
                conn.execute("UPDATE jobs SET status='failed' WHERE id=?",(original,))
            self.assertEqual(client.post(f"/api/v1/jobs/{original}/retry",json={"scope":"step"}).status_code,409)
            self.assertEqual(client.post(f"/api/v1/jobs/{original}/retry",json={"scope":"unknown"}).status_code,422)
            with api.get_db() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE character_id=?",(character_id,)).fetchone()[0],0)
                self.assertEqual(conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0],original)

    def test_cancelled_job_cannot_be_retried(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            job_id=client.post("/api/v1/media/image",json={"content":"portrait"}).get_json()["job_id"]
            self.assertEqual(client.post(f"/api/v1/jobs/{job_id}/cancel").status_code,204)
            detail=client.get(f"/api/v1/jobs/{job_id}").get_json()
            self.assertTrue(detail["cancelled"]);self.assertFalse(detail["can_retry"])
            self.assertEqual(client.post(f"/api/v1/jobs/{job_id}/retry").status_code,409)

    def test_cancel_queued_creation_removes_incomplete_character(self):
        with api.app.test_client() as client:
            made=client.post("/api/v1/characters",json={"name":"Blair","concept":"adult photographer","personality_notes":"curious and warm"}).get_json()
            self.assertEqual(client.post(f"/api/v1/jobs/{made['job_id']}/cancel").status_code,204)
            self.assertEqual(client.get(f"/api/v1/characters/{made['id']}").status_code,404)
            self.assertIsNone(client.get("/api/v1/bootstrap").get_json()["active_creation"])

    def test_cancel_running_image_interrupts_comfy_and_ignores_late_result(self):
        character_id=self.character()
        with api.app.test_client() as client,patch("vps.web_api.requests.post") as interrupt:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            job_id=client.post("/api/v1/media/image",json={"content":"portrait"}).get_json()["job_id"]
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(client.post(f"/api/v1/jobs/{job_id}/cancel").status_code,204)
            interrupt.assert_called_once();self.assertTrue(interrupt.call_args.args[0].endswith("/interrupt"))
            late=client.post(f"/jobs/{job_id}/complete",json={"lease_token":claimed["lease_token"],"ok":True,"image_b64":base64.b64encode(b"late").decode()})
            self.assertEqual(late.get_json()["status"],"ignored")
            self.assertEqual(client.get("/api/v1/bootstrap").get_json()["messages"],[])

    def test_settings_update_shared_runtime_values(self):
        with api.app.test_client() as client:
            response=client.patch("/api/v1/settings",json={
                "content_mode":"nsfw","aspect_mode":"cinematic",
                "character_videos":False,"model":"test-model",
                "endpoint_ollama":"http://192.168.1.4:11434/",
                "endpoint_comfyui":"http://192.168.1.5:8188",
            })
        self.assertEqual(response.status_code,200)
        settings=response.get_json()["settings"]
        self.assertEqual((settings["content_mode"],settings["aspect_mode"],settings["character_videos"],settings["model"]),("nsfw","cinematic",False,"test-model"))
        self.assertEqual(settings["endpoint_ollama"],"http://192.168.1.4:11434")
        self.assertEqual(settings["endpoint_comfyui"],"http://192.168.1.5:8188")
        with api.get_db() as conn:
            self.assertEqual(api.get_setting(conn,"current_model"),"test-model")

    def test_settings_reject_invalid_service_endpoint(self):
        with api.app.test_client() as client:
            response=client.patch("/api/v1/settings",json={"endpoint_comfyui":"not a url"})
        self.assertEqual(response.status_code,422)

    def test_diagnostics_require_debug_mode_and_exclude_private_data(self):
        character_id=self.character()
        with api.get_db() as conn:
            api.enqueue_job(conn,0,"character_image_edit","heavy",{"character_id":character_id,"prompt":"private prompt","reference_image":"D:/private/person.png"},context_id=None)
            job=conn.execute("SELECT id FROM jobs ORDER BY id DESC").fetchone()[0]
            conn.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",(json.dumps({"text":"RuntimeError at D:/private/person.png"}),job))
        probe=Mock();probe.ok=True;probe.status_code=200
        with api.app.test_client() as client:
            self.assertEqual(client.get("/api/v1/diagnostics").status_code,403)
            enabled=client.patch("/api/v1/settings",json={"debug_mode":True})
            self.assertEqual(enabled.status_code,200)
            self.assertTrue(enabled.get_json()["settings"]["debug_mode"])
            with patch("vps.web_api.requests.get",return_value=probe):
                response=client.get("/api/v1/diagnostics")
        self.assertEqual(response.status_code,200)
        report=response.get_json();serialized=response.get_data(as_text=True)
        self.assertIn("services",report);self.assertIn("jobs",report)
        self.assertNotIn("private prompt",serialized)
        self.assertNotIn("person.png",serialized)
        self.assertNotIn("TELEGRAM_BOT_TOKEN",serialized)

    def test_telegram_settings_are_write_only_and_persist_to_env(self):
        # Synthetic token, assembled so source scans do not mistake it for a credential.
        token="123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        with tempfile.TemporaryDirectory() as folder,patch("vps.web_api.ENV_PATH",Path(folder)/".env"),patch.dict(os.environ,{"TELEGRAM_ENABLED":"0","TELEGRAM_BOT_TOKEN":""},clear=False):
            with api.app.test_client() as client:
                saved=client.patch("/api/v1/settings",json={"telegram":{"enabled":True,"token":token}})
                self.assertEqual(saved.status_code,200)
                telegram=saved.get_json()["settings"]["telegram"]
                self.assertEqual(telegram,{"enabled":True,"configured":True,"active":False,"restart_required":True})
                self.assertNotIn(token,saved.get_data(as_text=True))
                kept=client.patch("/api/v1/settings",json={"telegram":{"enabled":True,"token":""}})
                self.assertEqual(kept.status_code,200)
                self.assertIn(f"TELEGRAM_BOT_TOKEN={token}",(Path(folder)/".env").read_text())
                removed=client.patch("/api/v1/settings",json={"telegram":{"enabled":False,"remove_token":True}})
                self.assertEqual(removed.status_code,200)
                self.assertIn("TELEGRAM_BOT_TOKEN=\n",(Path(folder)/".env").read_text())

    def test_telegram_connection_reports_bot_without_returning_token(self):
        # Synthetic token, assembled so source scans do not mistake it for a credential.
        token="123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        response=Mock();response.ok=True;response.json.return_value={"ok":True,"result":{"username":"cast_test_bot","first_name":"Cast Test"}}
        with patch("vps.web_api.requests.get",return_value=response):
            with api.app.test_client() as client:
                result=client.post("/api/v1/telegram/test",json={"token":token})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.get_json()["username"],"cast_test_bot")
        self.assertNotIn(token,result.get_data(as_text=True))

    def test_character_edit_regenerates_profile_without_telegram_delivery(self):
        character_id=self.character()
        with api.app.test_client() as client:
            client.put("/api/v1/active-character",json={"character_id":character_id})
            changed=client.patch(f"/api/v1/characters/{character_id}",json={
                "name":"Avery Two","concept":"new look","personality_notes":"new personality",
            })
            self.assertEqual(changed.status_code,200)
            job_id=changed.get_json()["job_id"]
            claimed=client.get("/jobs/next?worker_id=test").get_json()
            self.assertEqual(claimed["job_type"],"edit_profile_generate")
            client.post(f"/jobs/{job_id}/complete",json={
                "lease_token":claimed["lease_token"],"ok":True,"text":"New system profile",
            })
            detail=client.get(f"/api/v1/characters/{character_id}").get_json()
        self.assertEqual((detail["name"],detail["concept"],detail["personality_notes"]),("Avery Two","new look","new personality"))
        with api.get_db() as conn:
            self.assertEqual(conn.execute("SELECT system_prompt FROM characters WHERE id=?",(character_id,)).fetchone()[0],"New system profile")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)

    def test_character_delete_requires_exact_name(self):
        character_id=self.character()
        with api.app.test_client() as client:
            self.assertEqual(client.delete(f"/api/v1/characters/{character_id}",json={"confirm_name":"wrong"}).status_code,409)
            self.assertEqual(client.delete(f"/api/v1/characters/{character_id}",json={"confirm_name":"Avery"}).status_code,204)
            self.assertEqual(client.get(f"/api/v1/characters/{character_id}").status_code,404)

    def test_shell_and_health(self):
        with api.app.test_client() as client:
            page=client.get("/web");health=client.get("/api/v1/health")
        self.assertEqual(page.status_code,200)
        shell=page.get_data(as_text=True)
        self.assertIn("CastWorkshop",shell)
        self.assertIn('id="image-viewer"',shell)
        self.assertIn('id="image-view-mode"',shell)
        self.assertIn('id="job-progress"',shell)
        self.assertIn('id="job-retry"',shell)
        self.assertIn('id="diagnostics-dialog"',shell)
        self.assertIn('id="debug-open"',shell)
        self.assertEqual(health.get_json()["status"],"ok")

if __name__=="__main__":
    unittest.main()
