import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from worker.recovery import recover_videos
from worker.job_runtime import JobClient


class VideoRecoveryTests(unittest.TestCase):
    def response(self,value,ok=True,status_code=200):
        r=Mock();r.json.return_value=value;r.ok=ok;r.status_code=status_code;r.content=b""
        return r

    def job(self,job_type="character_video",endpoint="http://comfy",prompts=True):
        progress={"comfy_prompts":[{"prompt_id":"p","endpoint":endpoint}]} if prompts else {"comfy_prompts":[]}
        return {"id":1,"job_type":job_type,"lease_token":"token","progress_json":json.dumps(progress)}

    def test_completed_video_spooled_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            client=JobClient("http://api",directory);session=Mock()
            entry={"status":{"status_str":"success","completed":True},
                   "outputs":{"92":{"images":[{"filename":"clip.mp4","type":"output"}]}}}
            video=self.response({});video.content=b"test-video"
            session.get.side_effect=[self.response([self.job()]),self.response({"queue_running":[],"queue_pending":[]}),self.response({"p":entry}),video]
            recover_videos(client,"http://comfy",session)
            session.post.assert_not_called()
            path=next(Path(directory).glob("*.pending.json"));saved=json.loads(path.read_text())
            self.assertEqual(saved["body"]["lease_token"],"token")
            self.assertEqual(saved["url"],"http://api/jobs/1/complete")

    def test_missing_voice_prompt_is_safely_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            client=JobClient("http://api",directory);session=Mock()
            session.get.side_effect=[self.response([self.job("voice_sample_batch")]),self.response({"queue_running":[],"queue_pending":[]}),self.response({})]
            session.post.return_value=self.response({"status":"queued"})
            recover_videos(client,"http://comfy",session)
            session.post.assert_called_once_with("http://api/jobs/1/recover",json={"lease_token":"token"},timeout=10)

    def test_job_still_in_comfy_queue_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            client=JobClient("http://api",directory);session=Mock()
            session.get.side_effect=[self.response([self.job("voice_sample_batch")]),self.response({"queue_running":[[0,"p",{}]],"queue_pending":[]})]
            recover_videos(client,"http://comfy",session)
            session.post.assert_not_called()

    def test_other_comfy_endpoint_is_left_for_manual_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            client=JobClient("http://api",directory);session=Mock()
            session.get.side_effect=[self.response([self.job("voice_sample_batch","http://elsewhere")]),self.response({"queue_running":[],"queue_pending":[]})]
            recover_videos(client,"http://comfy",session)
            session.post.assert_not_called()

    def test_ambiguous_submission_is_never_requeued_even_with_old_pass_id(self):
        for prompts in (False, True):
            with self.subTest(prompts=prompts), tempfile.TemporaryDirectory() as directory:
                client=JobClient("http://api",directory);session=Mock()
                job=self.job("multi_character_video",prompts=prompts)
                progress=json.loads(job["progress_json"])
                progress["phase"]="submitting"
                job["progress_json"]=json.dumps(progress)
                session.get.side_effect=[self.response([job]),self.response({"queue_running":[],"queue_pending":[]})]
                recover_videos(client,"http://comfy",session)
                session.post.assert_not_called()
                self.assertEqual(session.get.call_count,2)

    def test_unidentified_job_is_not_requeued_while_comfy_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            client=JobClient("http://api",directory);session=Mock()
            session.get.side_effect=[self.response([self.job(prompts=False)]),
                                    self.response({"queue_running":[[0,"unknown",{}]],"queue_pending":[]})]
            recover_videos(client,"http://comfy",session)
            session.post.assert_not_called()

if __name__=="__main__":
    unittest.main()
